"""
Worker entry point — Distributed Queue Autoscaling Platform.

Lifecycle:
  1. Poll the job queue for a work item
  2. Mark job RUNNING in DynamoDB (first worker only)
  3. Send `messageCount` messages to the target queue in batches
  4. Atomically increment completedWorkers on the job record
  5. Last worker aggregates results and marks job COMPLETED
  6. Delete the job queue message
  7. Exit (ECS will scale-in as queue empties)
"""
import json
import logging
import math
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from metrics_emitter import emit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── AWS clients ───────────────────────────────────────────────────────────────
_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
_sqs = boto3.client("sqs", region_name=_region)
_ddb = boto3.resource("dynamodb", region_name=_region)
_s3 = boto3.client("s3", region_name=_region)

TABLE_NAME = os.environ["TABLE_NAME"]
JOB_QUEUE_URL = os.environ["JOB_QUEUE_URL"]
RESULTS_BUCKET = os.environ["RESULTS_BUCKET"]
TARGET_QUEUE_URL = os.environ["TARGET_QUEUE_URL"]

_table = _ddb.Table(TABLE_NAME)

SQS_BATCH_SIZE = 10  # SQS SendMessageBatch maximum


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SQS job queue ─────────────────────────────────────────────────────────────

def poll_job() -> Optional[tuple[dict, str]]:
    """Long-poll the job queue for one work item."""
    response = _sqs.receive_message(
        QueueUrl=JOB_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
        VisibilityTimeout=600,  # 10 min max job duration
    )
    messages = response.get("Messages", [])
    if not messages:
        return None
    msg = messages[0]
    return json.loads(msg["Body"]), msg["ReceiptHandle"]


def delete_message(receipt_handle: str) -> None:
    _sqs.delete_message(QueueUrl=JOB_QUEUE_URL, ReceiptHandle=receipt_handle)


# ── Message publishing ────────────────────────────────────────────────────────

def publish_messages(message_count: int, processing_time: int) -> tuple[int, int, float]:
    """
    Send `message_count` messages to the target queue in batches of 10.

    Each message body includes:
      - messageId: unique UUID
      - processingTime: ms of simulated work the consumer should perform
      - sentAt: ISO timestamp

    Returns (total_sent, total_failed, duration_seconds).
    """
    sent = 0
    failed = 0
    started = time.monotonic()

    batches = math.ceil(message_count / SQS_BATCH_SIZE)
    remaining = message_count

    for batch_num in range(batches):
        batch_size = min(SQS_BATCH_SIZE, remaining)
        entries = [
            {
                "Id": str(i),
                "MessageBody": json.dumps({
                    "messageId": str(uuid.uuid4()),
                    "processingTime": processing_time,
                    "sentAt": _now(),
                }),
            }
            for i in range(batch_size)
        ]

        response = _sqs.send_message_batch(
            QueueUrl=TARGET_QUEUE_URL,
            Entries=entries,
        )
        sent += len(response.get("Successful", []))
        failed += len(response.get("Failed", []))
        remaining -= batch_size

        if batch_num % 100 == 0 and batch_num > 0:
            log.info("Progress: %d/%d sent", sent, message_count)

    duration = time.monotonic() - started
    return sent, failed, duration


# ── DynamoDB ──────────────────────────────────────────────────────────────────

def mark_running(job: dict) -> None:
    """Set status=RUNNING and startedAt. Idempotent — safe if called by multiple workers."""
    try:
        _table.update_item(
            Key={"jobId": job["jobId"], "createdAt": job["createdAt"]},
            UpdateExpression="SET #s = :s, startedAt = :t",
            ConditionExpression="attribute_not_exists(startedAt)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "RUNNING", ":t": _now()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            pass  # Another worker already set it — that's fine
        else:
            raise


def report_worker_done(job: dict, worker_index: int, result: dict) -> int:
    """
    Atomically increment completedWorkers and store this worker's partial result.
    Returns the new completedWorkers count.
    """
    ddb_result = {
        k: Decimal(str(v)) if isinstance(v, float) else v
        for k, v in result.items()
        if v is not None
    }
    response = _table.update_item(
        Key={"jobId": job["jobId"], "createdAt": job["createdAt"]},
        UpdateExpression="ADD completedWorkers :one SET workerResults.#wi = :r",
        ExpressionAttributeNames={"#wi": str(worker_index)},
        ExpressionAttributeValues={":one": 1, ":r": ddb_result},
        ReturnValues="ALL_NEW",
    )
    return int(response["Attributes"]["completedWorkers"])


def mark_completed(job: dict, results: dict) -> None:
    ddb_results = {
        k: Decimal(str(v)) if isinstance(v, float) else v
        for k, v in results.items()
        if v is not None
    }
    _table.update_item(
        Key={"jobId": job["jobId"], "createdAt": job["createdAt"]},
        UpdateExpression="SET #s = :s, completedAt = :t, results = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "COMPLETED",
            ":t": _now(),
            ":r": ddb_results,
        },
    )


def mark_failed(job: dict, reason: str) -> None:
    _table.update_item(
        Key={"jobId": job["jobId"], "createdAt": job["createdAt"]},
        UpdateExpression="SET #s = :s, completedAt = :t, failureReason = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "FAILED", ":t": _now(), ":r": reason},
    )


def fetch_worker_results(job: dict) -> list[dict]:
    """Fetch all partial worker results from the job record."""
    response = _table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("jobId").eq(job["jobId"]),
        ScanIndexForward=False,
        Limit=1,
    )
    item = response["Items"][0]
    raw = item.get("workerResults", {})
    return list(raw.values())


# ── Result aggregation ────────────────────────────────────────────────────────

def aggregate_results(worker_results: list[dict]) -> dict:
    """
    Aggregate partial results from all workers:
      - totalSent / totalFailed: sum
      - durationSeconds: max (workers ran in parallel, job is done when last one finishes)
      - throughput: sum (parallel workers contribute independently)
      - avgProcessingMs: mean across workers
    """
    def _float(v) -> float:
        return float(v) if isinstance(v, Decimal) else v

    total_sent = sum(int(r.get("totalSent", 0)) for r in worker_results)
    total_failed = sum(int(r.get("totalFailed", 0)) for r in worker_results)
    duration = max(_float(r.get("durationSeconds", 0)) for r in worker_results)
    throughput = sum(_float(r.get("throughput", 0)) for r in worker_results)

    proc_times = [_float(r["avgProcessingMs"]) for r in worker_results if r.get("avgProcessingMs") is not None]
    avg_proc = sum(proc_times) / len(proc_times) if proc_times else None

    return {
        "totalSent": total_sent,
        "totalFailed": total_failed,
        "durationSeconds": round(duration, 3),
        "throughput": round(throughput, 2),
        "avgProcessingMs": round(avg_proc, 2) if avg_proc is not None else None,
    }


# ── S3 ────────────────────────────────────────────────────────────────────────

def write_worker_result(job_id: str, worker_index: int, result: dict) -> None:
    key = f"results/{job_id}/worker-{worker_index}.json"
    _s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=key,
        Body=json.dumps(result),
        ContentType="application/json",
    )
    log.info("Worker result written to s3://%s/%s", RESULTS_BUCKET, key)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_worker() -> None:
    log.info("Worker starting — polling %s", JOB_QUEUE_URL)

    result = poll_job()
    if result is None:
        log.info("Queue empty — exiting")
        sys.exit(0)

    job, receipt = result
    job_id = job["jobId"]
    worker_index = job.get("workerIndex", 0)
    worker_count = job.get("workerCount", 1)
    message_count = int(job["messageCount"])
    processing_time = int(job.get("processingTime", 0))

    log.info(
        "Picked up job %s worker %d/%d — sending %d messages",
        job_id, worker_index, worker_count, message_count,
    )

    try:
        mark_running(job)

        sent, failed, duration = publish_messages(message_count, processing_time)
        throughput = round(sent / duration, 2) if duration > 0 else 0.0
        avg_proc = float(processing_time) if processing_time > 0 else None

        worker_result = {
            "totalSent": sent,
            "totalFailed": failed,
            "durationSeconds": round(duration, 3),
            "throughput": throughput,
            "avgProcessingMs": avg_proc,
        }

        write_worker_result(job_id, worker_index, worker_result)
        emit(job_id, worker_index, os.environ.get("ECS_TASK_ID", "local"), worker_result)

        new_count = report_worker_done(job, worker_index, worker_result)
        log.info("Job %s: %d/%d workers done", job_id, new_count, worker_count)

        if new_count >= worker_count:
            # Last worker — aggregate and complete
            all_results = fetch_worker_results(job)
            aggregated = aggregate_results(all_results)
            log.info("Job %s aggregated results: %s", job_id, aggregated)
            mark_completed(job, aggregated)

        delete_message(receipt)
        log.info("Worker %d for job %s done", worker_index, job_id)

    except Exception as exc:
        log.exception("Worker %d for job %s failed: %s", worker_index, job_id, exc)
        try:
            mark_failed(job, str(exc))
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    run_worker()

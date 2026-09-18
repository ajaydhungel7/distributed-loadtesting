"""
Worker — Distributed Queue Autoscaling Platform.

Lifecycle:
  1. Poll loadtest-target queue for a message (long-poll, 20s)
  2. Enable ECS task scale-in protection so autoscaling won't kill us mid-job
  3. Simulate processing (sleep processingTime ms)
  4. Atomically increment processedCount on the job record
  5. If processedCount == messageCount: mark job COMPLETED with results
  6. Delete the message and release scale-in protection
  7. Repeat until MAX_EMPTY_POLLS consecutive empty polls → exit cleanly

Scale-in protection ensures ECS never terminates a task that is actively
processing a message. Workers self-report when it is safe to be stopped.
"""
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from metrics_emitter import emit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
_sqs = boto3.client("sqs", region_name=_region)
_ddb = boto3.resource("dynamodb", region_name=_region)
_s3 = boto3.client("s3", region_name=_region)

TABLE_NAME = os.environ["TABLE_NAME"]
TARGET_QUEUE_URL = os.environ["TARGET_QUEUE_URL"]
RESULTS_BUCKET = os.environ["RESULTS_BUCKET"]
# How many consecutive empty polls before the worker exits.
# With WaitTimeSeconds=20, this means ~60s of idle before scale-in.
MAX_EMPTY_POLLS = int(os.environ.get("MAX_EMPTY_POLLS", "3"))
# ECS agent endpoint injected automatically by ECS runtime.
_ECS_AGENT_URI = os.environ.get("ECS_AGENT_URI", "")

_table = _ddb.Table(TABLE_NAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SQS ───────────────────────────────────────────────────────────────────────

def poll_message() -> Optional[tuple[dict, str]]:
    """Long-poll the target queue for one message (20s wait = fewer empty requests)."""
    response = _sqs.receive_message(
        QueueUrl=TARGET_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,
        VisibilityTimeout=60,
    )
    messages = response.get("Messages", [])
    if not messages:
        return None
    msg = messages[0]
    return json.loads(msg["Body"]), msg["ReceiptHandle"]


def _set_scale_in_protection(enabled: bool) -> None:
    """Tell ECS whether this task is safe to terminate.

    While processing a message the task is protected; once done it releases
    the protection so autoscaling can scale it in when the queue drains.
    """
    if not _ECS_AGENT_URI:
        return
    try:
        data = json.dumps({"ProtectionEnabled": enabled}).encode()
        req = urllib.request.Request(
            f"{_ECS_AGENT_URI}/task-protection/v1/state",
            data=data, method="PUT",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        log.warning("Could not set scale-in protection=%s: %s", enabled, exc)


def delete_message(receipt_handle: str) -> None:
    _sqs.delete_message(QueueUrl=TARGET_QUEUE_URL, ReceiptHandle=receipt_handle)


# ── DynamoDB ──────────────────────────────────────────────────────────────────

def get_job(job_id: str) -> Optional[dict]:
    response = _table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("jobId").eq(job_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


def increment_processed(job_id: str, created_at: str) -> tuple[int, int]:
    """Atomically increment processedCount. Returns (new_count, message_count)."""
    response = _table.update_item(
        Key={"jobId": job_id, "createdAt": created_at},
        UpdateExpression="ADD processedCount :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="ALL_NEW",
    )
    attrs = response["Attributes"]
    return int(attrs["processedCount"]), int(attrs["messageCount"])


def mark_completed(job_id: str, created_at: str, results: dict) -> None:
    ddb_results = {
        k: Decimal(str(v)) if isinstance(v, float) else v
        for k, v in results.items()
        if v is not None
    }
    _table.update_item(
        Key={"jobId": job_id, "createdAt": created_at},
        UpdateExpression="SET #s = :s, completedAt = :t, results = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "COMPLETED",
            ":t": _now(),
            ":r": ddb_results,
        },
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run_worker() -> None:
    log.info("Worker starting — polling %s", TARGET_QUEUE_URL)

    empty_polls = 0
    while True:
        result = poll_message()

        if result is None:
            empty_polls += 1
            if empty_polls >= MAX_EMPTY_POLLS:
                log.info("Queue empty for %d consecutive polls — exiting", empty_polls)
                sys.exit(0)
            log.debug("Empty poll %d/%d", empty_polls, MAX_EMPTY_POLLS)
            continue

        empty_polls = 0
        message, receipt = result
        job_id = message.get("jobId", "unknown")
        processing_time_ms = int(message.get("processingTime", 0))

        log.info("Processing message for job %s (processingTime=%dms)", job_id, processing_time_ms)
        _set_scale_in_protection(True)
        try:
            # Simulate work
            if processing_time_ms > 0:
                time.sleep(processing_time_ms / 1000.0)

            # Fetch job to get createdAt
            job = get_job(job_id)
            if not job:
                log.warning("Job %s not found in DynamoDB — skipping", job_id)
                delete_message(receipt)
                continue

            created_at = job["createdAt"]
            processed_count, message_count = increment_processed(job_id, created_at)

            log.info("Job %s: %d/%d processed", job_id, processed_count, message_count)

            if processed_count >= message_count:
                results = {
                    "totalProcessed": processed_count,
                    "avgProcessingMs": float(processing_time_ms),
                }
                mark_completed(job_id, created_at, results)
                emit(job_id, os.environ.get("ECS_TASK_ID", "local"), results)
                log.info("Job %s COMPLETED", job_id)

            delete_message(receipt)

        except Exception as exc:
            log.exception("Failed processing message for job %s: %s", job_id, exc)
            # Don't delete — let SQS retry up to maxReceiveCount then DLQ
            sys.exit(1)
        finally:
            _set_scale_in_protection(False)


if __name__ == "__main__":
    run_worker()


"""
Worker — Distributed Queue Autoscaling Platform.

Lifecycle:
  1. Poll loadtest-target queue for a message
  2. Simulate processing (sleep processingTime ms)
  3. Atomically increment processedCount on the job record
  4. If processedCount == messageCount: mark job COMPLETED with results
  5. Delete the message
  6. Exit — ECS will restart the task or scale-in as queue empties
"""
import json
import logging
import os
import sys
import time
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

_table = _ddb.Table(TABLE_NAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SQS ───────────────────────────────────────────────────────────────────────

def poll_message() -> Optional[tuple[dict, str]]:
    """Long-poll the target queue for one message."""
    response = _sqs.receive_message(
        QueueUrl=TARGET_QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
        VisibilityTimeout=30,
    )
    messages = response.get("Messages", [])
    if not messages:
        return None
    msg = messages[0]
    return json.loads(msg["Body"]), msg["ReceiptHandle"]


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

    result = poll_message()
    if result is None:
        log.info("Queue empty — exiting")
        sys.exit(0)

    message, receipt = result
    job_id = message.get("jobId", "unknown")
    processing_time_ms = int(message.get("processingTime", 0))

    log.info("Processing message for job %s (processingTime=%dms)", job_id, processing_time_ms)

    try:
        start = time.monotonic()

        # Simulate work
        if processing_time_ms > 0:
            time.sleep(processing_time_ms / 1000.0)

        duration = time.monotonic() - start

        # Fetch job to get createdAt
        job = get_job(job_id)
        if not job:
            log.warning("Job %s not found in DynamoDB — skipping", job_id)
            delete_message(receipt)
            return

        created_at = job["createdAt"]
        processed_count, message_count = increment_processed(job_id, created_at)

        log.info("Job %s: %d/%d processed", job_id, processed_count, message_count)

        if processed_count >= message_count:
            # Last message — compute results and complete the job
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


if __name__ == "__main__":
    while True:
        run_worker()


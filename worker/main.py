"""
Worker entry point.

Lifecycle:
  1. Poll SQS for a job
  2. Mark job RUNNING in DynamoDB
  3. Run k6 against the target URL
  4. Write raw results to S3
  5. Write summary results + mark COMPLETED in DynamoDB
  6. Delete SQS message
  7. Exit (ECS will restart or scale-in as needed)
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import boto3

from k6_runner import build_k6_script, parse_k6_summary, run_k6
from metrics_emitter import emit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── AWS clients ───────────────────────────────────────────────────────────────
# No credentials here — boto3 uses the ECS task role on Fargate,
# or your local AWS CLI profile during development.
_region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
_sqs = boto3.client("sqs", region_name=_region)
_ddb = boto3.resource("dynamodb", region_name=_region)
_s3 = boto3.client("s3", region_name=_region)

TABLE_NAME = os.environ["TABLE_NAME"]
QUEUE_URL = os.environ["JOB_QUEUE_URL"]
RESULTS_BUCKET = os.environ["RESULTS_BUCKET"]

_table = _ddb.Table(TABLE_NAME)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SQS ───────────────────────────────────────────────────────────────────────

def poll_job() -> Optional[tuple[dict, str]]:
    """
    Long-poll SQS for one job message.
    Returns (job_dict, receipt_handle) or None if the queue is empty.
    """
    response = _sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=5,
        VisibilityTimeout=600,  # 10 min — matches CDK queue config
    )
    messages = response.get("Messages", [])
    if not messages:
        return None
    msg = messages[0]
    return json.loads(msg["Body"]), msg["ReceiptHandle"]


def delete_message(receipt_handle: str) -> None:
    _sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt_handle)


def extend_visibility(receipt_handle: str, seconds: int = 120) -> None:
    """Heartbeat — prevents the message from becoming visible again mid-test."""
    _sqs.change_message_visibility(
        QueueUrl=QUEUE_URL,
        ReceiptHandle=receipt_handle,
        VisibilityTimeout=seconds,
    )


# ── DynamoDB ──────────────────────────────────────────────────────────────────

def mark_running(job: dict) -> None:
    _table.update_item(
        Key={"testId": job["testId"], "createdAt": job["createdAt"]},
        UpdateExpression="SET #s = :s, startedAt = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "RUNNING", ":t": _now()},
    )


def mark_completed(job: dict, summary: dict) -> None:
    # DynamoDB resource requires Decimal for numeric types (not float)
    ddb_summary = {
        k: Decimal(str(v)) if isinstance(v, float) else v
        for k, v in summary.items()
        if v is not None
    }
    _table.update_item(
        Key={"testId": job["testId"], "createdAt": job["createdAt"]},
        UpdateExpression="SET #s = :s, completedAt = :t, results = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "COMPLETED",
            ":t": _now(),
            ":r": ddb_summary,
        },
    )


def mark_failed(job: dict, reason: str) -> None:
    _table.update_item(
        Key={"testId": job["testId"], "createdAt": job["createdAt"]},
        UpdateExpression="SET #s = :s, completedAt = :t, failureReason = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "FAILED", ":t": _now(), ":r": reason},
    )


# ── S3 ────────────────────────────────────────────────────────────────────────

def write_raw_results(test_id: str, raw: dict) -> None:
    key = f"results/{test_id}/raw.json"
    _s3.put_object(
        Bucket=RESULTS_BUCKET,
        Key=key,
        Body=json.dumps(raw),
        ContentType="application/json",
    )
    log.info("Raw results written to s3://%s/%s", RESULTS_BUCKET, key)


# ── Main ──────────────────────────────────────────────────────────────────────

def run_worker() -> None:
    log.info("Worker starting — polling %s", QUEUE_URL)

    result = poll_job()
    if result is None:
        log.info("Queue empty — exiting")
        sys.exit(0)

    job, receipt = result
    test_id = job["testId"]
    log.info("Picked up job %s (%s)", test_id, job.get("name"))

    try:
        mark_running(job)

        script = build_k6_script(
            target_url=job["targetUrl"],
            vus=int(job["virtualUsers"]),
            duration=job["duration"],
            ramp_up=job.get("rampUp"),
        )

        log.info("Running k6 for job %s", test_id)
        raw_summary = run_k6(script)

        write_raw_results(test_id, raw_summary)

        parsed = parse_k6_summary(raw_summary)
        log.info("Results for %s: %s", test_id, parsed)

        emit(test_id, os.environ.get("ECS_TASK_ID", "local"), parsed)
        mark_completed(job, parsed)
        delete_message(receipt)

        log.info("Job %s completed successfully", test_id)

    except Exception as exc:
        log.exception("Job %s failed: %s", test_id, exc)
        try:
            mark_failed(job, str(exc))
        except Exception:
            pass
        # Do NOT delete the message — let it retry up to maxReceiveCount (3),
        # then SQS moves it to the DLQ automatically.
        sys.exit(1)


if __name__ == "__main__":
    run_worker()

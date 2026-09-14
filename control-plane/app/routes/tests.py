import json
import math
import os
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, HTTPException
from ulid import ULID

from app.models.test_job import (
    CreateJobRequest,
    JobRecord,
    JobListResponse,
    JobStatus,
)
from app.services import dynamodb

router = APIRouter(prefix="/jobs", tags=["jobs"])

TARGET_QUEUE_URL = os.environ["TARGET_QUEUE_URL"]
_sqs = boto3.client("sqs", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

SQS_BATCH_SIZE = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _publish_messages(job_id: str, message_count: int, processing_time: int) -> None:
    """Publish all messages to the target queue in batches of 10."""
    remaining = message_count
    batch_num = 0
    while remaining > 0:
        batch_size = min(SQS_BATCH_SIZE, remaining)
        entries = [
            {
                "Id": str(i),
                "MessageBody": json.dumps({
                    "jobId": job_id,
                    "processingTime": processing_time,
                }),
            }
            for i in range(batch_size)
        ]
        _sqs.send_message_batch(QueueUrl=TARGET_QUEUE_URL, Entries=entries)
        remaining -= batch_size
        batch_num += 1


@router.post("", status_code=202)
def create_job(body: CreateJobRequest) -> dict:
    job_id = str(ULID())
    created_at = _now()

    item = {
        "jobId": job_id,
        "name": body.name,
        "messageCount": body.messageCount,
        "processingTime": body.processingTime,
        "status": JobStatus.PENDING.value,
        "createdAt": created_at,
        "startedAt": None,
        "completedAt": None,
        "processedCount": 0,
        "results": {},
    }

    dynamodb.put_item(item)

    # Flood the target queue — this triggers autoscaling
    _publish_messages(job_id, body.messageCount, body.processingTime)

    dynamodb.update_status(job_id, created_at, JobStatus.RUNNING.value, {"startedAt": created_at})

    return {"jobId": job_id, "status": JobStatus.RUNNING.value}


@router.get("/{job_id}")
def get_job(job_id: str) -> dict:
    item = dynamodb.get_item(job_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return item


@router.get("")
def list_jobs(status: str | None = None) -> JobListResponse:
    items = dynamodb.list_items(status=status)
    return JobListResponse(items=items, count=len(items))


@router.delete("/{job_id}")
def cancel_job(job_id: str) -> dict:
    item = dynamodb.get_item(job_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if item["status"] not in (JobStatus.PENDING.value, JobStatus.RUNNING.value):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a job in '{item['status']}' state",
        )
    dynamodb.update_status(job_id, item["createdAt"], JobStatus.CANCELLED.value)
    return {"jobId": job_id, "status": JobStatus.CANCELLED.value}

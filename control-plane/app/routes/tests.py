import math
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from ulid import ULID

from app.models.test_job import (
    CreateJobRequest,
    JobRecord,
    JobListResponse,
    JobStatus,
)
from app.services import dynamodb, sqs

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Max messages each worker will be asked to send
MESSAGES_PER_WORKER = int(os.environ.get("MESSAGES_PER_WORKER", "1000"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("", status_code=202)
def create_job(body: CreateJobRequest) -> dict:
    job_id = str(ULID())
    created_at = _now()

    # Fan-out: split messageCount across workers
    worker_count = math.ceil(body.messageCount / MESSAGES_PER_WORKER)
    base_messages = body.messageCount // worker_count
    remainder = body.messageCount % worker_count

    item = {
        "jobId": job_id,
        "name": body.name,
        "messageCount": body.messageCount,
        "processingTime": body.processingTime,
        "status": JobStatus.PENDING.value,
        "createdAt": created_at,
        "startedAt": None,
        "completedAt": None,
        "workerCount": worker_count,
        "completedWorkers": 0,
        "workerResults": {},
        "results": {},
    }

    dynamodb.put_item(item)

    # Each worker gets base_messages; first worker gets the remainder too
    for i in range(worker_count):
        worker_messages = base_messages + (remainder if i == 0 else 0)
        sqs.send_job({
            "jobId": job_id,
            "name": body.name,
            "messageCount": worker_messages,
            "processingTime": body.processingTime,
            "workerIndex": i,
            "workerCount": worker_count,
            "createdAt": created_at,
        })

    return {"jobId": job_id, "status": JobStatus.PENDING.value, "workerCount": worker_count}


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

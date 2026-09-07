from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from ulid import ULID

from app.models.test_job import (
    CreateTestRequest,
    TestJob,
    TestListResponse,
    TestStatus,
)
from app.services import dynamodb, sqs

router = APIRouter(prefix="/tests", tags=["tests"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("", status_code=202)
def create_test(body: CreateTestRequest) -> dict:
    # SSRF guard — validator raises ValueError; convert to 400 here
    parsed = urlparse(body.targetUrl)
    from app.models.test_job import _is_private_host
    if _is_private_host(parsed.hostname or ""):
        raise HTTPException(status_code=400, detail="targetUrl resolves to a private or reserved IP range")

    test_id = str(ULID())
    created_at = _now()

    item = {
        "testId": test_id,
        "name": body.name,
        "targetUrl": body.targetUrl,
        "virtualUsers": body.virtualUsers,
        "duration": body.duration,
        "rampUp": body.rampUp,
        "status": TestStatus.PENDING.value,
        "createdAt": created_at,
        "startedAt": None,
        "completedAt": None,
        "workerCount": 0,
        "results": {},
    }

    dynamodb.put_item(item)
    sqs.send_job({
        "testId": test_id,
        "name": body.name,
        "targetUrl": body.targetUrl,
        "virtualUsers": body.virtualUsers,
        "duration": body.duration,
        "rampUp": body.rampUp,
        "createdAt": created_at,
    })

    return {"testId": test_id, "status": TestStatus.PENDING.value}


@router.get("/{test_id}")
def get_test(test_id: str) -> dict:
    item = dynamodb.get_item(test_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Test '{test_id}' not found")
    return item


@router.get("")
def list_tests(status: str | None = None) -> TestListResponse:
    items = dynamodb.list_items(status=status)
    return TestListResponse(items=items, count=len(items))


@router.delete("/{test_id}")
def cancel_test(test_id: str) -> dict:
    item = dynamodb.get_item(test_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Test '{test_id}' not found")
    if item["status"] not in (TestStatus.PENDING.value, TestStatus.RUNNING.value):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a test in '{item['status']}' state",
        )
    dynamodb.update_status(test_id, item["createdAt"], TestStatus.CANCELLED.value)
    return {"testId": test_id, "status": TestStatus.CANCELLED.value}

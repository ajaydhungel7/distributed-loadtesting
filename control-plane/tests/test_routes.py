import json
import pytest


# ── POST /jobs ────────────────────────────────────────────────────────────────

def test_post_job_returns_202(client):
    c, sqs, queue_url = client
    response = c.post("/jobs", json={"name": "smoke-test", "messageCount": 10})
    assert response.status_code == 202


def test_post_job_returns_job_id_and_running_status(client):
    c, sqs, queue_url = client
    response = c.post("/jobs", json={"name": "smoke-test", "messageCount": 10})
    body = response.json()
    assert "jobId" in body
    assert body["status"] == "RUNNING"


def test_post_job_publishes_messages_to_target_queue(client):
    c, sqs, queue_url = client
    c.post("/jobs", json={"name": "flood-test", "messageCount": 25})

    # Drain the queue
    total = 0
    while True:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        msgs = resp.get("Messages", [])
        if not msgs:
            break
        total += len(msgs)
        for m in msgs:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])

    assert total == 25


def test_post_job_message_has_job_id_and_processing_time(client):
    c, sqs, queue_url = client
    c.post("/jobs", json={"name": "field-check", "messageCount": 1, "processingTime": 200})
    resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    body = json.loads(resp["Messages"][0]["Body"])
    assert "jobId" in body
    assert body["processingTime"] == 200


def test_post_job_rejects_zero_message_count(client):
    c, _, __ = client
    assert c.post("/jobs", json={"name": "bad", "messageCount": 0}).status_code == 422


def test_post_job_rejects_negative_processing_time(client):
    c, _, __ = client
    assert c.post("/jobs", json={"name": "bad", "messageCount": 10, "processingTime": -1}).status_code == 422


def test_post_job_default_processing_time_is_zero(client):
    c, sqs, queue_url = client
    c.post("/jobs", json={"name": "defaults", "messageCount": 1})
    resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    body = json.loads(resp["Messages"][0]["Body"])
    assert body["processingTime"] == 0


# ── GET /jobs/{id} ────────────────────────────────────────────────────────────

def test_get_job_returns_record(client):
    c, _, __ = client
    job_id = c.post("/jobs", json={"name": "my-job", "messageCount": 5}).json()["jobId"]
    body = c.get(f"/jobs/{job_id}").json()
    assert body["jobId"] == job_id
    assert body["messageCount"] == 5
    assert body["status"] == "RUNNING"


def test_get_job_returns_404_for_unknown(client):
    c, _, __ = client
    assert c.get("/jobs/nonexistent").status_code == 404


# ── GET /jobs ─────────────────────────────────────────────────────────────────

def test_list_jobs_returns_all(client):
    c, _, __ = client
    for i in range(3):
        c.post("/jobs", json={"name": f"job-{i}", "messageCount": 5})
    body = c.get("/jobs").json()
    assert body["count"] == 3


def test_list_jobs_filters_by_status(client):
    c, _, __ = client
    c.post("/jobs", json={"name": "job-1", "messageCount": 5})
    items = c.get("/jobs?status=RUNNING").json()["items"]
    assert all(item["status"] == "RUNNING" for item in items)


# ── DELETE /jobs/{id} ─────────────────────────────────────────────────────────

def test_cancel_running_job(client):
    c, _, __ = client
    job_id = c.post("/jobs", json={"name": "cancel-me", "messageCount": 5}).json()["jobId"]
    assert c.delete(f"/jobs/{job_id}").status_code == 200
    assert c.get(f"/jobs/{job_id}").json()["status"] == "CANCELLED"


def test_cancel_returns_404_for_unknown(client):
    c, _, __ = client
    assert c.delete("/jobs/nonexistent").status_code == 404


# ── GET /health ───────────────────────────────────────────────────────────────

def test_health_check(client):
    c, _, __ = client
    response = c.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

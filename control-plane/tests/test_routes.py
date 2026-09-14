import json
import pytest


# ── POST /jobs ────────────────────────────────────────────────────────────────

def test_post_job_returns_202(client):
    c, sqs, queue_url = client
    response = c.post("/jobs", json={
        "name": "smoke-test",
        "messageCount": 100,
    })
    assert response.status_code == 202


def test_post_job_returns_job_id_and_pending_status(client):
    c, sqs, queue_url = client
    response = c.post("/jobs", json={
        "name": "smoke-test",
        "messageCount": 100,
    })
    body = response.json()
    assert "jobId" in body
    assert body["status"] == "PENDING"


def test_post_job_returns_worker_count(client):
    c, sqs, queue_url = client
    # 2500 messages / 1000 per worker = 3 workers
    response = c.post("/jobs", json={
        "name": "big-job",
        "messageCount": 2500,
    })
    body = response.json()
    assert body["workerCount"] == 3


def test_post_job_publishes_one_message_to_sqs_per_worker(client):
    c, sqs, queue_url = client
    # 2500 messages → 3 workers → 3 SQS messages
    c.post("/jobs", json={"name": "fanout-test", "messageCount": 2500})

    received = []
    while True:
        resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=0)
        msgs = resp.get("Messages", [])
        if not msgs:
            break
        received.extend(msgs)

    assert len(received) == 3
    bodies = [json.loads(m["Body"]) for m in received]
    worker_indices = sorted(b["workerIndex"] for b in bodies)
    assert worker_indices == [0, 1, 2]


def test_post_job_sqs_message_has_required_fields(client):
    c, sqs, queue_url = client
    c.post("/jobs", json={"name": "field-check", "messageCount": 100})
    resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    body = json.loads(resp["Messages"][0]["Body"])
    for field in ("jobId", "messageCount", "processingTime", "workerIndex", "workerCount", "createdAt"):
        assert field in body


def test_post_job_single_worker_for_small_count(client):
    c, sqs, queue_url = client
    response = c.post("/jobs", json={"name": "tiny", "messageCount": 50})
    assert response.json()["workerCount"] == 1


def test_post_job_rejects_zero_message_count(client):
    c, _, __ = client
    response = c.post("/jobs", json={"name": "bad", "messageCount": 0})
    assert response.status_code == 422


def test_post_job_rejects_negative_processing_time(client):
    c, _, __ = client
    response = c.post("/jobs", json={"name": "bad", "messageCount": 10, "processingTime": -1})
    assert response.status_code == 422


def test_post_job_default_processing_time_is_zero(client):
    c, sqs, queue_url = client
    c.post("/jobs", json={"name": "defaults", "messageCount": 10})
    resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    body = json.loads(resp["Messages"][0]["Body"])
    assert body["processingTime"] == 0


# ── GET /jobs/{id} ────────────────────────────────────────────────────────────

def test_get_job_by_id_returns_full_record(client):
    c, _, __ = client
    create_resp = c.post("/jobs", json={"name": "my-job", "messageCount": 500})
    job_id = create_resp.json()["jobId"]

    get_resp = c.get(f"/jobs/{job_id}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["jobId"] == job_id
    assert body["status"] == "PENDING"
    assert body["name"] == "my-job"
    assert body["messageCount"] == 500


def test_get_job_returns_404_for_unknown(client):
    c, _, __ = client
    assert c.get("/jobs/nonexistent-id").status_code == 404


# ── GET /jobs ─────────────────────────────────────────────────────────────────

def test_list_jobs_returns_all(client):
    c, _, __ = client
    for i in range(3):
        c.post("/jobs", json={"name": f"job-{i}", "messageCount": 10})
    response = c.get("/jobs")
    assert response.status_code == 200
    assert response.json()["count"] == 3


def test_list_jobs_filters_by_status(client):
    c, _, __ = client
    c.post("/jobs", json={"name": "job-1", "messageCount": 10})
    response = c.get("/jobs?status=PENDING")
    items = response.json()["items"]
    assert all(item["status"] == "PENDING" for item in items)


# ── DELETE /jobs/{id} ─────────────────────────────────────────────────────────

def test_cancel_pending_job(client):
    c, _, __ = client
    create_resp = c.post("/jobs", json={"name": "cancel-me", "messageCount": 10})
    job_id = create_resp.json()["jobId"]

    delete_resp = c.delete(f"/jobs/{job_id}")
    assert delete_resp.status_code == 200

    get_resp = c.get(f"/jobs/{job_id}")
    assert get_resp.json()["status"] == "CANCELLED"


def test_cancel_returns_404_for_unknown(client):
    c, _, __ = client
    assert c.delete("/jobs/nonexistent-id").status_code == 404


# ── GET /health ───────────────────────────────────────────────────────────────

def test_health_check(client):
    c, _, __ = client
    response = c.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

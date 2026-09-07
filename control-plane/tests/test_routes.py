import json
import pytest


# ── POST /tests ───────────────────────────────────────────────────────────────

def test_post_tests_returns_202(client):
    c, sqs, queue_url = client
    payload = {
        "name": "smoke-test",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 10,
        "duration": "30s",
    }
    response = c.post("/tests", json=payload)
    assert response.status_code == 202


def test_post_tests_returns_test_id_and_pending_status(client):
    c, sqs, queue_url = client
    payload = {
        "name": "smoke-test",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 10,
        "duration": "30s",
    }
    response = c.post("/tests", json=payload)
    body = response.json()
    assert "testId" in body
    assert body["status"] == "PENDING"


def test_post_tests_publishes_message_to_sqs(client):
    c, sqs, queue_url = client
    payload = {
        "name": "smoke-test",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 10,
        "duration": "30s",
    }
    c.post("/tests", json=payload)
    messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    assert "Messages" in messages
    body = json.loads(messages["Messages"][0]["Body"])
    assert body["targetUrl"] == "https://httpbin.org/get"
    assert body["virtualUsers"] == 10


def test_post_tests_rejects_invalid_url(client):
    c, _, __ = client
    response = c.post("/tests", json={
        "name": "bad",
        "targetUrl": "not-a-url",
        "virtualUsers": 10,
        "duration": "30s",
    })
    assert response.status_code == 422


def test_post_tests_rejects_private_ip_ssrf(client):
    c, _, __ = client
    response = c.post("/tests", json={
        "name": "ssrf",
        "targetUrl": "http://169.254.169.254/latest/meta-data/",
        "virtualUsers": 1,
        "duration": "5s",
    })
    assert response.status_code == 400
    assert "private" in response.json()["detail"].lower()


def test_post_tests_rejects_zero_virtual_users(client):
    c, _, __ = client
    response = c.post("/tests", json={
        "name": "bad",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 0,
        "duration": "30s",
    })
    assert response.status_code == 422


def test_post_tests_rejects_too_many_virtual_users(client):
    c, _, __ = client
    response = c.post("/tests", json={
        "name": "bad",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 10001,
        "duration": "30s",
    })
    assert response.status_code == 422


def test_post_tests_rejects_invalid_duration_format(client):
    c, _, __ = client
    response = c.post("/tests", json={
        "name": "bad",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 10,
        "duration": "forever",
    })
    assert response.status_code == 422


# ── GET /tests/{id} ───────────────────────────────────────────────────────────

def test_get_test_by_id_returns_full_record(client):
    c, _, __ = client
    create_resp = c.post("/tests", json={
        "name": "my-test",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 50,
        "duration": "1m",
    })
    test_id = create_resp.json()["testId"]

    get_resp = c.get(f"/tests/{test_id}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["testId"] == test_id
    assert body["status"] == "PENDING"
    assert body["name"] == "my-test"
    assert body["targetUrl"] == "https://httpbin.org/get"
    assert body["virtualUsers"] == 50


def test_get_test_by_id_returns_404_for_unknown(client):
    c, _, __ = client
    response = c.get("/tests/nonexistent-id")
    assert response.status_code == 404


# ── GET /tests ────────────────────────────────────────────────────────────────

def test_list_tests_returns_all_tests(client):
    c, _, __ = client
    for i in range(3):
        c.post("/tests", json={
            "name": f"test-{i}",
            "targetUrl": "https://httpbin.org/get",
            "virtualUsers": 10,
            "duration": "30s",
        })
    response = c.get("/tests")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3


def test_list_tests_filters_by_status(client):
    c, _, __ = client
    c.post("/tests", json={
        "name": "test-1",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 10,
        "duration": "30s",
    })
    response = c.get("/tests?status=PENDING")
    assert response.status_code == 200
    items = response.json()["items"]
    assert all(item["status"] == "PENDING" for item in items)


# ── DELETE /tests/{id} ────────────────────────────────────────────────────────

def test_delete_test_cancels_pending_job(client):
    c, _, __ = client
    create_resp = c.post("/tests", json={
        "name": "cancel-me",
        "targetUrl": "https://httpbin.org/get",
        "virtualUsers": 10,
        "duration": "30s",
    })
    test_id = create_resp.json()["testId"]

    delete_resp = c.delete(f"/tests/{test_id}")
    assert delete_resp.status_code == 200

    get_resp = c.get(f"/tests/{test_id}")
    assert get_resp.json()["status"] == "CANCELLED"


def test_delete_test_returns_404_for_unknown(client):
    c, _, __ = client
    response = c.delete("/tests/nonexistent-id")
    assert response.status_code == 404


# ── GET /health ───────────────────────────────────────────────────────────────

def test_health_check_returns_200(client):
    c, _, __ = client
    response = c.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

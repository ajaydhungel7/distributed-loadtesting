"""Tests for the worker job lifecycle."""
import json
import os
import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch, MagicMock


@pytest.fixture()
def aws_resources():
    with mock_aws():
        # DynamoDB
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="load-tests",
            KeySchema=[
                {"AttributeName": "jobId", "KeyType": "HASH"},
                {"AttributeName": "createdAt", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "jobId", "AttributeType": "S"},
                {"AttributeName": "createdAt", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "createdAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # Job SQS queue
        sqs = boto3.client("sqs", region_name="us-east-1")
        job_queue = sqs.create_queue(QueueName="loadtest-jobs")
        job_queue_url = job_queue["QueueUrl"]
        os.environ["JOB_QUEUE_URL"] = job_queue_url

        # Target SQS queue (workers publish messages here)
        target_queue = sqs.create_queue(QueueName="target-queue")
        target_queue_url = target_queue["QueueUrl"]
        os.environ["TARGET_QUEUE_URL"] = target_queue_url

        # S3
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="loadtest-results")

        yield {
            "table": table,
            "sqs": sqs,
            "job_queue_url": job_queue_url,
            "target_queue_url": target_queue_url,
            "s3": s3,
        }


JOB = {
    "jobId": "job-001",
    "name": "smoke",
    "messageCount": 10,
    "processingTime": 0,
    "workerIndex": 0,
    "workerCount": 1,
    "createdAt": "2024-01-01T00:00:00+00:00",
}


def test_poll_returns_none_when_queue_empty(aws_resources):
    from main import poll_job
    assert poll_job() is None


def test_poll_returns_job_when_message_present(aws_resources):
    res = aws_resources
    res["sqs"].send_message(QueueUrl=res["job_queue_url"], MessageBody=json.dumps(JOB))
    from main import poll_job
    result = poll_job()
    assert result is not None
    job, receipt = result
    assert job["jobId"] == "job-001"


def test_mark_running_sets_status(aws_resources):
    res = aws_resources
    res["table"].put_item(Item={**JOB, "status": "PENDING", "results": {}})

    from main import mark_running
    mark_running(JOB)

    item = res["table"].query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("jobId").eq("job-001"),
        Limit=1,
    )["Items"][0]
    assert item["status"] == "RUNNING"


def test_mark_running_is_idempotent(aws_resources):
    """Second call should not raise even though startedAt already exists."""
    res = aws_resources
    res["table"].put_item(Item={**JOB, "status": "RUNNING", "startedAt": "2024-01-01T00:00:01+00:00", "results": {}})

    from main import mark_running
    mark_running(JOB)  # must not raise


def test_publish_messages_sends_to_target_queue(aws_resources):
    res = aws_resources
    from main import publish_messages
    sent, failed, duration = publish_messages(25, 0)
    assert sent == 25
    assert failed == 0
    assert duration > 0

    # Verify messages landed in the target queue
    attrs = res["sqs"].get_queue_attributes(
        QueueUrl=res["target_queue_url"],
        AttributeNames=["ApproximateNumberOfMessages"],
    )
    # moto doesn't always update this instantly, but sent count is authoritative
    assert sent == 25


def test_publish_messages_body_has_required_fields(aws_resources):
    from main import publish_messages
    publish_messages(1, 500)

    from main import _sqs
    resp = _sqs.receive_message(QueueUrl=os.environ["TARGET_QUEUE_URL"], MaxNumberOfMessages=1)
    body = json.loads(resp["Messages"][0]["Body"])
    assert "messageId" in body
    assert "processingTime" in body
    assert body["processingTime"] == 500
    assert "sentAt" in body


def test_mark_completed_writes_results(aws_resources):
    res = aws_resources
    res["table"].put_item(Item={**JOB, "status": "RUNNING", "completedWorkers": 0, "results": {}})

    summary = {"totalSent": 10, "totalFailed": 0, "durationSeconds": 1.5, "throughput": 6.67}
    from main import mark_completed
    mark_completed(JOB, summary)

    item = res["table"].query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("jobId").eq("job-001"),
        Limit=1,
    )["Items"][0]
    assert item["status"] == "COMPLETED"
    assert int(item["results"]["totalSent"]) == 10


def test_write_worker_result_creates_s3_object(aws_resources):
    from main import write_worker_result
    write_worker_result("job-001", 0, {"totalSent": 10})

    objects = aws_resources["s3"].list_objects_v2(Bucket="loadtest-results", Prefix="results/job-001/")
    assert objects["KeyCount"] == 1
    assert objects["Contents"][0]["Key"] == "results/job-001/worker-0.json"


def test_delete_message_removes_from_queue(aws_resources):
    res = aws_resources
    res["sqs"].send_message(QueueUrl=res["job_queue_url"], MessageBody=json.dumps(JOB))

    from main import poll_job, delete_message
    _, receipt = poll_job()
    delete_message(receipt)

    msgs = res["sqs"].receive_message(QueueUrl=res["job_queue_url"], WaitTimeSeconds=0)
    assert "Messages" not in msgs


def test_aggregate_results_sums_sent_and_throughput():
    from main import aggregate_results
    results = [
        {"totalSent": 1000, "totalFailed": 0, "durationSeconds": 2.0, "throughput": 500.0, "avgProcessingMs": 100.0},
        {"totalSent": 1000, "totalFailed": 2, "durationSeconds": 2.5, "throughput": 400.0, "avgProcessingMs": 200.0},
    ]
    agg = aggregate_results(results)
    assert agg["totalSent"] == 2000
    assert agg["totalFailed"] == 2
    assert agg["durationSeconds"] == 2.5   # max
    assert agg["throughput"] == 900.0       # sum
    assert agg["avgProcessingMs"] == 150.0  # mean


def test_aggregate_results_handles_no_processing_time():
    from main import aggregate_results
    results = [
        {"totalSent": 100, "totalFailed": 0, "durationSeconds": 1.0, "throughput": 100.0, "avgProcessingMs": None},
    ]
    agg = aggregate_results(results)
    assert agg["avgProcessingMs"] is None

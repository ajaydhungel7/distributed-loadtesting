"""Tests for the SQS job lifecycle — poll, process, write results."""
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
                {"AttributeName": "testId", "KeyType": "HASH"},
                {"AttributeName": "createdAt", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "testId", "AttributeType": "S"},
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

        # SQS
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue = sqs.create_queue(QueueName="loadtest-jobs")
        queue_url = queue["QueueUrl"]
        os.environ["JOB_QUEUE_URL"] = queue_url

        # S3
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="loadtest-results")

        yield {"table": table, "sqs": sqs, "queue_url": queue_url, "s3": s3}


JOB = {
    "testId": "test-001",
    "name": "smoke",
    "targetUrl": "https://httpbin.org/get",
    "virtualUsers": 10,
    "duration": "30s",
    "rampUp": None,
    "createdAt": "2024-01-01T00:00:00+00:00",
}

FAKE_K6_SUMMARY = {
    "metrics": {
        "http_req_duration": {
            "type": "trend",
            "values": {"p(50)": 42.0, "p(95)": 110.0, "p(99)": 190.0},
        },
        "http_req_failed": {"type": "rate", "values": {"rate": 0.005}},
        "http_reqs": {"type": "counter", "values": {"count": 300, "rate": 10.0}},
    }
}


def test_poll_returns_none_when_queue_empty(aws_resources):
    from main import poll_job
    result = poll_job()
    assert result is None


def test_poll_returns_job_when_message_present(aws_resources):
    res = aws_resources
    res["sqs"].send_message(QueueUrl=res["queue_url"], MessageBody=json.dumps(JOB))

    from main import poll_job
    result = poll_job()
    assert result is not None
    job, receipt = result
    assert job["testId"] == "test-001"


def test_job_sets_status_running_in_dynamodb(aws_resources):
    res = aws_resources
    # Pre-create the item in DynamoDB (control plane would have done this)
    res["table"].put_item(Item={**JOB, "status": "PENDING", "results": {}})
    res["sqs"].send_message(QueueUrl=res["queue_url"], MessageBody=json.dumps(JOB))

    from main import poll_job, mark_running
    result = poll_job()
    job, receipt = result
    mark_running(job)

    item = res["table"].query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("testId").eq("test-001"),
        Limit=1,
    )["Items"][0]
    assert item["status"] == "RUNNING"


def test_job_writes_results_to_dynamodb(aws_resources):
    res = aws_resources
    res["table"].put_item(Item={**JOB, "status": "RUNNING", "results": {}})

    summary = {"p50": 42.0, "p95": 110.0, "p99": 190.0, "errorRate": 0.005, "throughput": 10.0}

    from main import mark_completed
    mark_completed(JOB, summary)

    item = res["table"].query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("testId").eq("test-001"),
        Limit=1,
    )["Items"][0]
    assert item["status"] == "COMPLETED"
    assert float(item["results"]["p50"]) == pytest.approx(42.0)


def test_job_writes_raw_results_to_s3(aws_resources):
    res = aws_resources
    res["table"].put_item(Item={**JOB, "status": "RUNNING", "results": {}})

    from main import write_raw_results
    write_raw_results("test-001", FAKE_K6_SUMMARY)

    objects = res["s3"].list_objects_v2(Bucket="loadtest-results", Prefix="results/test-001/")
    assert objects["KeyCount"] == 1
    assert objects["Contents"][0]["Key"] == "results/test-001/raw.json"


def test_job_deletes_sqs_message_on_success(aws_resources):
    res = aws_resources
    res["table"].put_item(Item={**JOB, "status": "PENDING", "results": {}})
    res["sqs"].send_message(QueueUrl=res["queue_url"], MessageBody=json.dumps(JOB))

    from main import poll_job, delete_message
    result = poll_job()
    _, receipt = result
    delete_message(receipt)

    # Queue should now be empty
    msgs = res["sqs"].receive_message(QueueUrl=res["queue_url"], WaitTimeSeconds=0)
    assert "Messages" not in msgs

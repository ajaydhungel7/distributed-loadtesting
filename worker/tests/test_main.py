"""Tests for the worker message processing lifecycle."""
import json
import os
import boto3
import pytest
from moto import mock_aws


JOB = {
    "jobId": "job-001",
    "name": "smoke",
    "messageCount": 5,
    "processingTime": 0,
    "status": "RUNNING",
    "createdAt": "2024-01-01T00:00:00+00:00",
    "processedCount": 0,
    "results": {},
}

MESSAGE = {
    "jobId": "job-001",
    "processingTime": 0,
}


def test_poll_returns_none_when_queue_empty(aws_resources):
    from main import poll_message
    assert poll_message() is None


def test_poll_returns_message_when_present(aws_resources):
    res = aws_resources
    res["sqs"].send_message(QueueUrl=res["queue_url"], MessageBody=json.dumps(MESSAGE))
    from main import poll_message
    result = poll_message()
    assert result is not None
    msg, receipt = result
    assert msg["jobId"] == "job-001"


def test_increment_processed_returns_new_count(aws_resources):
    res = aws_resources
    res["table"].put_item(Item=JOB)
    from main import increment_processed
    count, total = increment_processed("job-001", JOB["createdAt"])
    assert count == 1
    assert total == 5


def test_increment_processed_multiple_times(aws_resources):
    res = aws_resources
    res["table"].put_item(Item=JOB)
    from main import increment_processed
    for i in range(1, 4):
        count, total = increment_processed("job-001", JOB["createdAt"])
        assert count == i
        assert total == 5


def test_mark_completed_writes_results(aws_resources):
    res = aws_resources
    res["table"].put_item(Item=JOB)
    from main import mark_completed
    mark_completed("job-001", JOB["createdAt"], {"totalProcessed": 5, "avgProcessingMs": 0.0})
    item = res["table"].query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("jobId").eq("job-001"),
        Limit=1,
    )["Items"][0]
    assert item["status"] == "COMPLETED"
    assert int(item["results"]["totalProcessed"]) == 5


def test_delete_message_removes_from_queue(aws_resources):
    res = aws_resources
    res["sqs"].send_message(QueueUrl=res["queue_url"], MessageBody=json.dumps(MESSAGE))
    from main import poll_message, delete_message
    _, receipt = poll_message()
    delete_message(receipt)
    msgs = res["sqs"].receive_message(QueueUrl=res["queue_url"], WaitTimeSeconds=0)
    assert "Messages" not in msgs


def test_get_job_returns_record(aws_resources):
    res = aws_resources
    res["table"].put_item(Item=JOB)
    from main import get_job
    job = get_job("job-001")
    assert job is not None
    assert job["jobId"] == "job-001"


def test_get_job_returns_none_for_unknown(aws_resources):
    from main import get_job
    assert get_job("nonexistent") is None

import os
import boto3
import pytest
from moto import mock_aws
from fastapi.testclient import TestClient

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TABLE_NAME", "load-tests")
os.environ.setdefault("JOB_QUEUE_URL", "placeholder")


@pytest.fixture(autouse=True)
def mock_aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture()
def dynamodb_table():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
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
        yield


@pytest.fixture()
def sqs_queue():
    with mock_aws():
        sqs = boto3.client("sqs", region_name="us-east-1")
        response = sqs.create_queue(QueueName="loadtest-jobs")
        queue_url = response["QueueUrl"]
        os.environ["JOB_QUEUE_URL"] = queue_url
        yield sqs, queue_url


@pytest.fixture()
def client(dynamodb_table, sqs_queue):
    from app.main import app
    with TestClient(app) as c:
        yield c, sqs_queue[0], sqs_queue[1]

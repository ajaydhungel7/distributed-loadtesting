import os
import boto3
import pytest
from moto import mock_aws

# Application config — not credentials
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TABLE_NAME", "queue-jobs")
os.environ.setdefault("RESULTS_BUCKET", "loadtest-results")
os.environ.setdefault("JOB_QUEUE_URL", "placeholder")  # overwritten per fixture


@pytest.fixture(autouse=True)
def mock_aws_credentials(monkeypatch):
    """
    Inject throwaway credentials only while moto is active.
    These are never sent to real AWS — moto intercepts all calls.
    When running outside tests, boto3 uses your local AWS CLI profile.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture()
def aws_resources():
    with mock_aws():
        # DynamoDB
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="queue-jobs",
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

import json
import os

import boto3

QUEUE_URL = os.environ["JOB_QUEUE_URL"]
_client = boto3.client("sqs", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


def send_job(job: dict) -> str:
    response = _client.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(job),
    )
    return response["MessageId"]

import os
from decimal import Decimal
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["TABLE_NAME"]
_resource = boto3.resource("dynamodb", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
_table = _resource.Table(TABLE_NAME)


def put_item(item: dict) -> None:
    _table.put_item(Item=item)


def _deserialize(item: dict) -> dict:
    """Convert DynamoDB Decimal types to int/float for JSON serialization."""
    result = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            result[k] = int(v) if v == v.to_integral_value() else float(v)
        elif isinstance(v, dict):
            result[k] = _deserialize(v)
        else:
            result[k] = v
    return result


def get_item(job_id: str) -> Optional[dict]:
    """Fetch the most recent record for a jobId."""
    response = _table.query(
        KeyConditionExpression=Key("jobId").eq(job_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return _deserialize(items[0]) if items else None


def update_status(job_id: str, created_at: str, status: str, extra: Optional[dict] = None) -> None:
    update_expr = "SET #s = :s"
    expr_names = {"#s": "status"}
    expr_values = {":s": status}

    if extra:
        for k, v in extra.items():
            update_expr += f", #{k} = :{k}"
            expr_names[f"#{k}"] = k
            expr_values[f":{k}"] = v

    _table.update_item(
        Key={"jobId": job_id, "createdAt": created_at},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def list_items(status: Optional[str] = None) -> list[dict]:
    if status:
        response = _table.query(
            IndexName="status-index",
            KeyConditionExpression=Key("status").eq(status),
        )
    else:
        response = _table.scan()
    return [_deserialize(item) for item in response.get("Items", [])]

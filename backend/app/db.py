"""boto3 DynamoDB resource + single-table helpers.

Single-table design: PK = USER#<id>, SK = <TYPE>#<item_id>.
GSI1 (GSI1PK = PK, GSI1SK = UPDATED#<iso_timestamp>#<item_id>) serves the
"everything changed since timestamp X" query used by the sync endpoint.
"""
import os
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

# Single hardcoded account -- no users table, see docs/BACKEND_ARCHITECTURE.md.
USER_ID = "me"
TABLE_NAME = os.environ.get("TABLE_NAME", "yaadi")

_resource = None


def _table():
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb")
    return _resource.Table(TABLE_NAME)


def user_pk() -> str:
    return f"USER#{USER_ID}"


def note_sk(note_id: str) -> str:
    return f"NOTE#{note_id}"


def folder_sk(folder_id: str) -> str:
    return f"FOLDER#{folder_id}"


def attachment_sk(attachment_id: str) -> str:
    return f"ATTACHMENT#{attachment_id}"


def gsi1_sk(updated_at: str, item_id: str) -> str:
    return f"UPDATED#{updated_at}#{item_id}"


def put_item(item: dict) -> None:
    _table().put_item(Item=item)


def get_item(sk: str) -> Optional[dict]:
    resp = _table().get_item(Key={"PK": user_pk(), "SK": sk})
    return resp.get("Item")


def query_by_prefix(sk_prefix: str) -> list[dict]:
    key_condition = Key("PK").eq(user_pk()) & Key("SK").begins_with(sk_prefix)
    resp = _table().query(KeyConditionExpression=key_condition)
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = _table().query(
            KeyConditionExpression=key_condition,
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    return items


def query_updated_since(since: str) -> list[dict]:
    key_condition = Key("GSI1PK").eq(user_pk()) & Key("GSI1SK").gt(f"UPDATED#{since}")
    resp = _table().query(IndexName="GSI1", KeyConditionExpression=key_condition)
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = _table().query(
            IndexName="GSI1",
            KeyConditionExpression=key_condition,
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    return items

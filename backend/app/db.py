"""boto3 DynamoDB resource + single-table helpers.

Single-table design: PK = USER#<id>, SK = <TYPE>#<item_id>.
GSI1 (GSI1PK = PK, GSI1SK = UPDATED#<iso_timestamp>#<item_id>) serves the
"everything changed since timestamp X" query used by the sync endpoint.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key

# Single hardcoded account -- no users table, see docs/BACKEND_ARCHITECTURE.md.
USER_ID = "me"
TABLE_NAME = os.environ.get("TABLE_NAME", "yaadi")

# One throttle item per client IP: a global counter would let anyone who can
# reach /auth/login lock the owner out just by failing 5 times.
# shortcut: items for IPs that never log in successfully are never deleted
# (tiny; add a DynamoDB TTL if the table ever fills up with them).
def login_throttle_sk(ip: str) -> str:
    return f"LOGIN_THROTTLE#{ip}"

_resource = None


def _table():
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb")
    return _resource.Table(TABLE_NAME)


def now() -> str:
    # Fixed microsecond precision: timestamps are compared as strings, and
    # isoformat() silently drops ".000000" when microsecond == 0.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def user_pk() -> str:
    return f"USER#{USER_ID}"


def note_sk(note_id: str) -> str:
    return f"NOTE#{note_id}"


def folder_sk(folder_id: str) -> str:
    return f"FOLDER#{folder_id}"


def attachment_sk(attachment_id: str) -> str:
    return f"ATTACHMENT#{attachment_id}"


# Vault items deliberately never get GSI1 keys, so /sync/pull (which only walks
# GSI1) can't return them -- see vault.py.
def vault_item_sk(item_id: str) -> str:
    return f"VAULT#ITEM#{item_id}"


VAULT_PIN_SK = "VAULT#PIN"
VAULT_GLOBAL_THROTTLE_SK = "VAULT_THROTTLE#global"


def vault_ip_throttle_sk(ip: str) -> str:
    return f"VAULT_THROTTLE#{ip}"


def vault_audit_sk(at: str, event_id: str) -> str:
    return f"VAULT#AUDIT#{at}#{event_id}"


def gsi1_sk(updated_at: str, item_id: str) -> str:
    return f"UPDATED#{updated_at}#{item_id}"


def put_item(item: dict) -> None:
    _table().put_item(Item=item)


def put_if_newer(item: dict) -> bool:
    """Writes `item` only if nothing is stored under its key yet or the stored
    `updated_at` is strictly older -- one atomic call, so two devices pushing
    the same item at once can't have the older write land last.
    Returns False (nothing written) if the stored copy is as new or newer."""
    try:
        _table().put_item(
            Item=item,
            ConditionExpression=Attr("SK").not_exists() | Attr("updated_at").lt(item["updated_at"]),
        )
        return True
    except _table().meta.client.exceptions.ConditionalCheckFailedException:
        return False


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


def get_login_throttle(ip: str) -> Optional[dict]:
    return get_item(login_throttle_sk(ip))


def record_failure(sk: str, lockout_threshold: int, lockout_durations_seconds: list[int]) -> Optional[str]:
    """Atomically bumps the failure counter stored under `sk`. Once it reaches
    `lockout_threshold`, escalates into a lockout (duration taken from
    `lockout_durations_seconds`, indexed by how many times this has
    happened before and capped at the last entry) and resets the counter.

    Returns the new `locked_until` timestamp if a lockout was just
    triggered, else None.
    """
    resp = _table().update_item(
        Key={"PK": user_pk(), "SK": sk},
        UpdateExpression="ADD failed_count :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    failed_count = int(resp["Attributes"]["failed_count"])
    if failed_count < lockout_threshold:
        return None

    item = get_item(sk) or {}
    lockout_count = int(item.get("lockout_count", 0)) + 1
    duration = lockout_durations_seconds[min(lockout_count - 1, len(lockout_durations_seconds) - 1)]
    locked_until = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat(timespec="microseconds")

    _table().update_item(
        Key={"PK": user_pk(), "SK": sk},
        UpdateExpression="SET locked_until = :lu, lockout_count = :lc, failed_count = :zero",
        ExpressionAttributeValues={":lu": locked_until, ":lc": lockout_count, ":zero": 0},
    )
    return locked_until


def record_login_failure(ip: str, lockout_threshold: int, lockout_durations_seconds: list[int]) -> Optional[str]:
    return record_failure(login_throttle_sk(ip), lockout_threshold, lockout_durations_seconds)


def delete_item(sk: str) -> None:
    _table().delete_item(Key={"PK": user_pk(), "SK": sk})


def clear_login_throttle(ip: str) -> None:
    delete_item(login_throttle_sk(ip))

#!/usr/bin/env python3
"""Set (or reset) the 6-digit vault PIN. Run locally with your AWS credentials.

- Hashes the PIN with argon2 and writes it to the DynamoDB item VAULT#PIN.
  The Lambda reads it fresh on every unlock, so a new PIN works immediately.
- Clears any vault lockout, so this is also the way back in after one.
- Creates the vault key (SSM /yaadi/vault_key, 32 random bytes) the first
  time only. That key is what the vault is encrypted with: it is never
  rotated here, because a new key would make everything already in the vault
  unreadable. Back it up somewhere safe (it's needed to read the vault).
"""
import argparse
import base64
import getpass
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from argon2 import PasswordHasher
from boto3.dynamodb.conditions import Key

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from security import VAULT_KEY_PARAM, is_weak_pin  # noqa: E402

PK = "USER#me"


def generate_pin() -> str:
    while True:
        pin = f"{secrets.randbelow(1_000_000):06d}"
        if not is_weak_pin(pin):
            return pin


def ask_pin() -> str:
    pin = getpass.getpass("New 6-digit vault PIN: ")
    if len(pin) != 6 or not pin.isdigit():
        print("The PIN must be exactly 6 digits.", file=sys.stderr)
        sys.exit(1)
    if is_weak_pin(pin):
        print("That PIN is too easy to guess (repeated digits, a run like 123456, or a common pick).", file=sys.stderr)
        sys.exit(1)
    if getpass.getpass("Confirm PIN: ") != pin:
        print("The PINs don't match.", file=sys.stderr)
        sys.exit(1)
    return pin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=None, help="AWS region (defaults to your configured region)")
    parser.add_argument("--table", default="yaadi", help="DynamoDB table name (default: yaadi)")
    parser.add_argument("--generate", action="store_true", help="Pick a random strong PIN and print it once, instead of asking")
    args = parser.parse_args()

    if args.generate:
        pin = generate_pin()
    else:
        pin = ask_pin()

    ssm = boto3.client("ssm", region_name=args.region)
    try:
        ssm.get_parameter(Name=VAULT_KEY_PARAM, WithDecryption=True)
        print(f"Vault key already exists at {VAULT_KEY_PARAM} (kept as is)")
    except ssm.exceptions.ParameterNotFound:
        key = base64.b64encode(secrets.token_bytes(32)).decode()
        ssm.put_parameter(Name=VAULT_KEY_PARAM, Value=key, Type="SecureString")
        print(f"Created the vault key at {VAULT_KEY_PARAM}. Back it up: without it the vault can't be read.")

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    table.put_item(Item={"PK": PK, "SK": "VAULT#PIN", "hash": PasswordHasher().hash(pin), "updated_at": now})
    print(f"Saved the new vault PIN: {pin}" if args.generate else "Saved the new vault PIN")

    # Lockouts are keyed by IP plus one global item; clear them all.
    resp = table.query(
        KeyConditionExpression=Key("PK").eq(PK) & Key("SK").begins_with("VAULT_THROTTLE#")
    )
    for item in resp.get("Items", []):
        table.delete_item(Key={"PK": PK, "SK": item["SK"]})
    if resp.get("Items"):
        print(f"Cleared {len(resp['Items'])} vault lockout record(s)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-off bootstrap: hash the Yaadi password and store it (plus a JWT signing
secret, generated if missing) in SSM Parameter Store. Run once, locally, with
your AWS credentials. Re-run any time to change the password.
"""
import argparse
import getpass
import secrets
import sys

import boto3
from argon2 import PasswordHasher

PASSWORD_PARAM = "/yaadi/password_hash"
JWT_SECRET_PARAM = "/yaadi/jwt_secret"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=None, help="AWS region (defaults to your configured region)")
    args = parser.parse_args()

    password = getpass.getpass("New Yaadi password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords don't match.", file=sys.stderr)
        sys.exit(1)
    if not password:
        print("Password can't be empty.", file=sys.stderr)
        sys.exit(1)

    ssm = boto3.client("ssm", region_name=args.region)
    password_hash = PasswordHasher().hash(password)

    ssm.put_parameter(
        Name=PASSWORD_PARAM, Value=password_hash, Type="SecureString", Overwrite=True
    )
    print(f"Wrote password hash to {PASSWORD_PARAM}")

    existing = ssm.get_parameters(Names=[JWT_SECRET_PARAM], WithDecryption=True)["Parameters"]
    if existing:
        print(f"{JWT_SECRET_PARAM} already set, leaving it unchanged.")
    else:
        jwt_secret = secrets.token_urlsafe(64)
        ssm.put_parameter(Name=JWT_SECRET_PARAM, Value=jwt_secret, Type="SecureString")
        print(f"Generated and wrote a new JWT signing secret to {JWT_SECRET_PARAM}")


if __name__ == "__main__":
    main()

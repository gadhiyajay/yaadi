#!/usr/bin/env python3
"""One-off bootstrap: hash the Yaadi password and store it, plus a fresh JWT
signing secret, in SSM Parameter Store. Run once, locally, with your AWS
credentials. Re-run any time to change the password -- that also rotates the
JWT secret, which signs every device out (the running Lambda picks the new
secret up on its next cold start).
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

    # Always rotated: a password change has to end every existing session.
    ssm.put_parameter(
        Name=JWT_SECRET_PARAM, Value=secrets.token_urlsafe(64), Type="SecureString", Overwrite=True
    )
    print(f"Wrote a new JWT signing secret to {JWT_SECRET_PARAM} (all devices are signed out)")


if __name__ == "__main__":
    main()

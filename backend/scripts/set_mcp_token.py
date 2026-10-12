#!/usr/bin/env python3
"""Create (or rotate) the bearer token Claude uses to reach /mcp, and store
only its SHA-256 in SSM Parameter Store. Run locally with your AWS
credentials. The token is printed once -- copy it into your MCP client config.

Re-running it revokes the old token immediately (the Lambda reads this
parameter on every MCP request, it isn't cached). Separate from the login
password and JWT secret, so neither change affects the other.
"""
import argparse
import hashlib
import secrets
import tomllib
from pathlib import Path

import boto3

MCP_TOKEN_HASH_PARAM = "/yaadi/mcp_token_hash"
MCP_URL = "https://api.yaadi.jaylabs.uk/mcp"
SAMCONFIG = Path(__file__).resolve().parent.parent / "samconfig.toml"


def _deployed_region() -> str:
    # The token has to land in the region the Lambda runs in, not wherever
    # the local AWS profile happens to point (or nowhere, if it has none).
    with SAMCONFIG.open("rb") as f:
        return tomllib.load(f)["default"]["deploy"]["parameters"]["region"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=None, help="AWS region (defaults to the one in samconfig.toml)")
    args = parser.parse_args()

    token = secrets.token_urlsafe(32)
    region = args.region or _deployed_region()
    boto3.client("ssm", region_name=region).put_parameter(
        Name=MCP_TOKEN_HASH_PARAM,
        Value=hashlib.sha256(token.encode()).hexdigest(),
        Type="SecureString",
        Overwrite=True,
    )
    print(f"Wrote token hash to {MCP_TOKEN_HASH_PARAM} in {region} (any previous token is revoked).\n")
    print(f"Token (shown once): {token}\n")
    print("Add it to Claude Code with:")
    print(f'  claude mcp add --transport http yaadi {MCP_URL} --header "Authorization: Bearer {token}"')


if __name__ == "__main__":
    main()

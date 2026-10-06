import os
import sys
from pathlib import Path

import boto3
import pytest
from argon2 import PasswordHasher
from moto import mock_aws

# app/ is flattened into the Lambda root at deploy time (CodeUri: app/), so
# the app modules use flat sibling imports, not an `app.` package prefix --
# mirror that here by putting app/ on sys.path instead of importing `app.x`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

os.environ.setdefault("TABLE_NAME", "yaadi-test")
os.environ.setdefault("ATTACHMENTS_BUCKET", "yaadi-test-attachments")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def aws(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=os.environ["TABLE_NAME"],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=os.environ["ATTACHMENTS_BUCKET"])

        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(
            Name="/yaadi/password_hash",
            Value=PasswordHasher().hash(TEST_PASSWORD),
            Type="SecureString",
        )
        ssm.put_parameter(
            Name="/yaadi/jwt_secret",
            Value="test-jwt-secret",
            Type="SecureString",
        )

        # force the module-level boto3 clients/caches in app code to rebind
        # against the mocked AWS backend for this test.
        import db
        import security

        db._resource = None
        security._ssm_client = None
        security._param_cache.clear()

        yield


@pytest.fixture
def client(aws):
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app)


@pytest.fixture
def auth_headers(client):
    resp = client.post("/auth/login", json={"password": TEST_PASSWORD})
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}

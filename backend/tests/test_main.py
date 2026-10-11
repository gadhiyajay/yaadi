import importlib

import boto3
import pytest
from fastapi.testclient import TestClient

ORIGIN_SECRET = "test-origin-secret"


@pytest.fixture
def lambda_app(aws, monkeypatch):
    """main.app as it runs in Lambda (docs off, Worker secret enforced)."""
    import main

    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/yaadi/origin_secret", Value=ORIGIN_SECRET, Type="SecureString"
    )
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "YaadiFunction")
    try:
        yield TestClient(importlib.reload(main).app)
    finally:
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME")
        importlib.reload(main)


def test_docs_are_disabled_in_lambda(lambda_app):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert lambda_app.get(path, headers={"X-Origin-Secret": ORIGIN_SECRET}).status_code == 404


def test_docs_are_enabled_locally(client):
    assert client.get("/docs").status_code == 200


def test_lambda_rejects_requests_not_from_the_worker(lambda_app):
    assert lambda_app.get("/health").status_code == 403
    assert lambda_app.get("/health", headers={"X-Origin-Secret": "wrong"}).status_code == 403
    assert lambda_app.post("/auth/login", json={"password": "x"}).status_code == 403
    assert lambda_app.get("/health", headers={"X-Origin-Secret": ORIGIN_SECRET}).status_code == 200


def test_secret_is_not_enforced_locally(client):
    assert client.get("/health").status_code == 200

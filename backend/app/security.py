"""argon2 password hashing, JWT encode/decode, SSM secret cache.

The password hash and JWT signing secret live in SSM Parameter Store
(set once via scripts/set_password.py) and are cached in memory for the
Lambda execution context's lifetime -- see docs/BACKEND_ARCHITECTURE.md.
"""
import time

import boto3
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

PASSWORD_HASH_PARAM = "/yaadi/password_hash"
JWT_SECRET_PARAM = "/yaadi/jwt_secret"

ACCESS_TOKEN_EXPIRY_SECONDS = 15 * 60
REFRESH_TOKEN_EXPIRY_SECONDS = 30 * 24 * 60 * 60

JWT_ALGORITHM = "HS256"

_ssm_client = None
_param_cache: dict[str, str] = {}
_hasher = PasswordHasher()


def _ssm():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _get_ssm_param(name: str) -> str:
    if name not in _param_cache:
        resp = _ssm().get_parameter(Name=name, WithDecryption=True)
        _param_cache[name] = resp["Parameter"]["Value"]
    return _param_cache[name]


def get_password_hash() -> str:
    return _get_ssm_param(PASSWORD_HASH_PARAM)


def get_jwt_secret() -> str:
    return _get_ssm_param(JWT_SECRET_PARAM)


def verify_password(password: str) -> bool:
    try:
        return _hasher.verify(get_password_hash(), password)
    except VerifyMismatchError:
        return False


def _create_token(token_type: str, expires_in_seconds: int) -> str:
    now = int(time.time())
    payload = {"type": token_type, "iat": now, "exp": now + expires_in_seconds}
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


def create_access_token() -> str:
    return _create_token("access", ACCESS_TOKEN_EXPIRY_SECONDS)


def create_refresh_token() -> str:
    return _create_token("refresh", REFRESH_TOKEN_EXPIRY_SECONDS)


def decode_token(token: str, expected_type: str) -> dict:
    payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected a {expected_type} token")
    return payload

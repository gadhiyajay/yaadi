"""argon2 password hashing, JWT encode/decode, SSM secret cache.

The password hash and JWT signing secret live in SSM Parameter Store
(set once via scripts/set_password.py) and are cached in memory for the
Lambda execution context's lifetime -- see docs/BACKEND_ARCHITECTURE.md.
"""
import hashlib
import hmac
import time

import boto3
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

PASSWORD_HASH_PARAM = "/yaadi/password_hash"
JWT_SECRET_PARAM = "/yaadi/jwt_secret"
# Shared with the Cloudflare Worker (yaadi-app/worker.js) -- see main.py.
ORIGIN_SECRET_PARAM = "/yaadi/origin_secret"
MCP_TOKEN_HASH_PARAM = "/yaadi/mcp_token_hash"
# 32 random bytes (base64) the browser encrypts the vault with; released only
# after a correct vault PIN. Written once by scripts/set_vault_pin.py.
VAULT_KEY_PARAM = "/yaadi/vault_key"

ACCESS_TOKEN_EXPIRY_SECONDS = 15 * 60
REFRESH_TOKEN_EXPIRY_SECONDS = 30 * 24 * 60 * 60
# Deliberately short: the vault asks for the PIN again after this, no extension.
VAULT_TOKEN_EXPIRY_SECONDS = 5 * 60

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


def get_origin_secret() -> str:
    return _get_ssm_param(ORIGIN_SECRET_PARAM)


def verify_mcp_token(token: str) -> bool:
    """The MCP bearer token (scripts/set_mcp_token.py) -- only its SHA-256 is
    stored. Deliberately not cached like the other params: re-running the
    script has to revoke the old token now, not at the next cold start."""
    try:
        resp = _ssm().get_parameter(Name=MCP_TOKEN_HASH_PARAM, WithDecryption=True)
    except _ssm().exceptions.ParameterNotFound:
        return False  # no token set yet == MCP disabled
    sent = hashlib.sha256(token.encode()).hexdigest()
    return hmac.compare_digest(sent, resp["Parameter"]["Value"])


def get_vault_key() -> str:
    return _get_ssm_param(VAULT_KEY_PARAM)


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_hash(stored_hash: str, secret: str) -> bool:
    try:
        return _hasher.verify(stored_hash, secret)
    except VerifyMismatchError:
        return False


# The handful of PINs people pick first. A 6-digit space is small enough
# already; these would fall to the first few guesses of any attacker.
_COMMON_PINS = {"123123", "112233", "121212", "123321", "696969", "159753", "147258", "111222", "102030", "200000"}


def is_weak_pin(pin: str) -> bool:
    """All one digit, a straight run up or down (012345, 987654), or a common pick."""
    if len(set(pin)) == 1 or pin in _COMMON_PINS:
        return True
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    return steps in ({1}, {-1})


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


def create_vault_token() -> str:
    return _create_token("vault", VAULT_TOKEN_EXPIRY_SECONDS)


def decode_token(token: str, expected_type: str) -> dict:
    payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected a {expected_type} token")
    return payload

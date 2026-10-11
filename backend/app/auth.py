"""POST /auth/login, POST /auth/refresh -- JWT issuing/validation.

Single hardcoded account, see docs/BACKEND_ARCHITECTURE.md -- no registration
endpoint, no users table.
"""
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import db
import security
from models import LoginRequest, RefreshRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer_scheme = HTTPBearer()

# After this many consecutive failed /auth/login attempts, further attempts
# are rejected without even checking the password until the lockout
# expires. Duration grows each time the threshold is hit again (1m, 5m,
# 30m, 2h, capped at 24h) so sustained guessing gets exponentially slower
# instead of just waiting out a fixed cooldown.
LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_DURATIONS_SECONDS = [60, 300, 1800, 7200, 86400]


def _client_ip(request: Request) -> str:
    # In Lambda every request carries the Worker's secret (main.py), so
    # X-Client-IP is the Worker's own copy of CF-Connecting-IP, not something
    # a caller can spoof. Locally there's no Worker; use the socket address.
    return request.headers.get("x-client-ip") or (request.client.host if request.client else "unknown")


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request) -> TokenResponse:
    ip = _client_ip(request)
    throttle = db.get_login_throttle(ip)
    if throttle and throttle.get("locked_until", "") > db.now():
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    if not security.verify_password(body.password):
        db.record_login_failure(ip, LOGIN_LOCKOUT_THRESHOLD, LOGIN_LOCKOUT_DURATIONS_SECONDS)
        raise HTTPException(status_code=401, detail="Invalid password")

    db.clear_login_throttle(ip)
    return TokenResponse(
        access_token=security.create_access_token(),
        refresh_token=security.create_refresh_token(),
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest) -> TokenResponse:
    try:
        security.decode_token(body.refresh_token, "refresh")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    return TokenResponse(
        access_token=security.create_access_token(),
        refresh_token=security.create_refresh_token(),
    )


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme)) -> None:
    """FastAPI dependency: raises 401 unless a valid access token is presented."""
    try:
        security.decode_token(credentials.credentials, "access")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")

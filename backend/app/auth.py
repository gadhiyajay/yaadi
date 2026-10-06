"""POST /auth/login, POST /auth/refresh -- JWT issuing/validation.

Single hardcoded account, see docs/BACKEND_ARCHITECTURE.md -- no registration
endpoint, no users table.
"""
import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import security
from models import LoginRequest, RefreshRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer_scheme = HTTPBearer()


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest) -> TokenResponse:
    if not security.verify_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid password")
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

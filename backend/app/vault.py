"""/vault -- the personal/family details space behind a second lock.

Two layers (see PLAN.md):
- Gate: a fixed 6-digit PIN, checked here on every unlock (argon2 hash in
  DynamoDB, set by scripts/set_vault_pin.py). A correct PIN returns a
  5-minute vault token and the vault key; there is no way to extend a vault
  session, the browser asks for the PIN again.
- Seal: items are AES-GCM encrypted in the browser with that key before they
  are sent here, so this table only ever stores ciphertext.

A 6-digit PIN is only a million guesses, so failures are limited twice: per
client IP (so a stranger can't lock the owner out from elsewhere) and
globally (so many IPs can't share the million between them). Every attempt
already needs a valid Yaadi access token.

Vault items never get GSI1 keys, so /sync/pull can't return them, and
mcp_server.py only ever touches notes/folders.

Wrong PIN and "vault locked" are 403, never 401: the app's apiFetch treats a
401 as an expired login and replays the request after refreshing, which
would submit every wrong PIN twice.
"""
import re
import time
import uuid

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request

import db
import security
from auth import _client_ip, get_current_user
from models import (
    VaultAuditEvent,
    VaultItem,
    VaultPinChange,
    VaultPutResult,
    VaultStatus,
    VaultUnlockRequest,
    VaultUnlockResponse,
)

router = APIRouter(prefix="/vault", tags=["vault"], dependencies=[Depends(get_current_user)])

PIN_PATTERN = re.compile(r"^\d{6}$")

IP_LOCKOUT_THRESHOLD = 3
IP_LOCKOUT_DURATIONS_SECONDS = [60, 300, 1800, 7200, 86400]
GLOBAL_LOCKOUT_THRESHOLD = 10
GLOBAL_LOCKOUT_DURATIONS_SECONDS = [3600, 6 * 3600, 86400]

# Well under DynamoDB's 400 KB item limit (base64 ciphertext of the JSON).
MAX_CIPHERTEXT_CHARS = 350_000
AUDIT_KEEP_SECONDS = 90 * 24 * 3600
AUDIT_LIST_LIMIT = 30


def require_vault_token(x_vault_token: str = Header(default="")) -> None:
    """Vault data routes need this on top of the access token."""
    try:
        security.decode_token(x_vault_token, "vault")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=403, detail="Vault is locked")


def _audit(event: str, request: Request) -> None:
    at = db.now()
    db.put_item(
        {
            "PK": db.user_pk(),
            "SK": db.vault_audit_sk(at, uuid.uuid4().hex[:8]),
            "event": event,
            "at": at,
            "ip": _client_ip(request),
            "user_agent": request.headers.get("user-agent", "")[:200],
            "ttl": int(time.time()) + AUDIT_KEEP_SECONDS,
        }
    )


def _locked_until(ip: str) -> str | None:
    now = db.now()
    locks = [
        (db.get_item(sk) or {}).get("locked_until", "")
        for sk in (db.vault_ip_throttle_sk(ip), db.VAULT_GLOBAL_THROTTLE_SK)
    ]
    active = [lock for lock in locks if lock > now]
    return max(active) if active else None


def _check_pin(pin: str, request: Request) -> None:
    """Raises 429 while locked out, 403 (and counts a failure) on a wrong PIN."""
    ip = _client_ip(request)
    locked = _locked_until(ip)
    if locked:
        raise HTTPException(status_code=429, detail={"message": "Too many wrong PINs. Try again later.", "locked_until": locked})

    stored = db.get_item(db.VAULT_PIN_SK)
    if not stored:
        raise HTTPException(status_code=409, detail="The vault isn't set up yet. Run scripts/set_vault_pin.py.")

    if PIN_PATTERN.fullmatch(pin) and security.verify_hash(stored["hash"], pin):
        db.delete_item(db.vault_ip_throttle_sk(ip))
        db.delete_item(db.VAULT_GLOBAL_THROTTLE_SK)
        return

    ip_lock = db.record_failure(db.vault_ip_throttle_sk(ip), IP_LOCKOUT_THRESHOLD, IP_LOCKOUT_DURATIONS_SECONDS)
    global_lock = db.record_failure(
        db.VAULT_GLOBAL_THROTTLE_SK, GLOBAL_LOCKOUT_THRESHOLD, GLOBAL_LOCKOUT_DURATIONS_SECONDS
    )
    _audit("unlock_failed", request)
    if ip_lock or global_lock:
        raise HTTPException(
            status_code=429,
            detail={"message": "Too many wrong PINs. Try again later.", "locked_until": max(filter(None, [ip_lock, global_lock]))},
        )
    failed = int((db.get_item(db.vault_ip_throttle_sk(ip)) or {}).get("failed_count", 0))
    raise HTTPException(status_code=403, detail={"message": "Wrong PIN", "attempts_left": IP_LOCKOUT_THRESHOLD - failed})


@router.get("/status", response_model=VaultStatus)
def status(request: Request) -> VaultStatus:
    return VaultStatus(configured=db.get_item(db.VAULT_PIN_SK) is not None, locked_until=_locked_until(_client_ip(request)))


@router.post("/unlock", response_model=VaultUnlockResponse)
def unlock(body: VaultUnlockRequest, request: Request) -> VaultUnlockResponse:
    _check_pin(body.pin, request)
    _audit("unlocked", request)
    return VaultUnlockResponse(
        vault_token=security.create_vault_token(),
        expires_in=security.VAULT_TOKEN_EXPIRY_SECONDS,
        key=security.get_vault_key(),
    )


def item_to_vault(item: dict) -> VaultItem:
    return VaultItem(
        id=item["SK"].removeprefix("VAULT#ITEM#"),
        ct=item["ct"],
        iv=item["iv"],
        updated_at=item["updated_at"],
        deleted_at=item.get("deleted_at"),
    )


def vault_to_item(v: VaultItem) -> dict:
    # No GSI1PK/GSI1SK on purpose: keeps vault items out of /sync/pull.
    item = {"PK": db.user_pk(), "SK": db.vault_item_sk(v.id), "ct": v.ct, "iv": v.iv, "updated_at": v.updated_at}
    if v.deleted_at:
        item["deleted_at"] = v.deleted_at
    return item


@router.get("/items", response_model=list[VaultItem], dependencies=[Depends(require_vault_token)])
def list_items() -> list[VaultItem]:
    # Tombstones included, so a delete made on one device reaches the others.
    return [item_to_vault(i) for i in db.query_by_prefix("VAULT#ITEM#")]


@router.put("/items/{item_id}", response_model=VaultPutResult, dependencies=[Depends(require_vault_token)])
def put_item(item_id: str, body: VaultItem) -> VaultPutResult:
    if body.id != item_id:
        raise HTTPException(status_code=400, detail="Item id doesn't match the URL")
    if len(body.ct) > MAX_CIPHERTEXT_CHARS:
        raise HTTPException(status_code=413, detail="This entry is too large to save")
    # Same rule as /sync/push: a device clock running ahead can't stamp the future.
    now = db.now()
    if body.updated_at > now:
        body = body.model_copy(update={"updated_at": now})
    if db.put_if_newer(vault_to_item(body)):
        return VaultPutResult(status="accepted")
    existing = db.get_item(db.vault_item_sk(item_id))
    return VaultPutResult(status="rejected", server_item=item_to_vault(existing))


@router.post("/pin", status_code=204, dependencies=[Depends(require_vault_token)])
def change_pin(body: VaultPinChange, request: Request) -> None:
    _check_pin(body.current_pin, request)
    if not PIN_PATTERN.fullmatch(body.new_pin):
        raise HTTPException(status_code=422, detail="The PIN must be exactly 6 digits")
    if security.is_weak_pin(body.new_pin):
        raise HTTPException(status_code=422, detail="That PIN is too easy to guess. Pick another.")
    db.put_item({"PK": db.user_pk(), "SK": db.VAULT_PIN_SK, "hash": security.hash_secret(body.new_pin), "updated_at": db.now()})
    _audit("pin_changed", request)


@router.get("/audit", response_model=list[VaultAuditEvent], dependencies=[Depends(require_vault_token)])
def audit() -> list[VaultAuditEvent]:
    items = db.query_by_prefix("VAULT#AUDIT#")
    newest = sorted(items, key=lambda i: i["SK"], reverse=True)[:AUDIT_LIST_LIMIT]
    return [VaultAuditEvent(event=i["event"], at=i["at"], ip=i["ip"], user_agent=i.get("user_agent", "")) for i in newest]

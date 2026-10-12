# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

FastAPI backend for Yaadi, a single-user note-taking app. Deployed as one AWS Lambda function (via Mangum) behind a Lambda Function URL — no API Gateway, no users table, no multi-tenant logic. The frontend PWA lives in the sibling repo `yaadi-app`.

## Commands

All commands run from `backend/`.

```bash
# install
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt

# test (AWS fully mocked via moto — no real credentials needed)
pytest
pytest tests/test_auth.py
pytest tests/test_auth.py::test_login_locks_out_after_repeated_failures

# deploy (needs real AWS credentials + samconfig.toml configured)
sam build && sam deploy
# or: ./scripts/deploy.ps1

# rotate/set the login password + JWT secret in SSM (one-off, local, needs AWS creds)
python scripts/set_password.py

# create/rotate the MCP bearer token (prints it once; revokes the previous one)
python scripts/set_mcp_token.py
```

There's no separate lint command configured; match existing style (no formatter/linter enforced in CI).

## Architecture

**Request flow**: Browser/PWA → `api.yaadi.jaylabs.uk` → Cloudflare Worker (`yaadi-app/worker.js`) → Lambda Function URL → Mangum → FastAPI app → DynamoDB / S3 / SSM Parameter Store. The Worker adds `X-Origin-Secret` (SSM `/yaadi/origin_secret`, same value as the Worker secret `ORIGIN_SECRET`) and `X-Client-IP`; the middleware in `main.py` 403s any request in Lambda without the secret, so the raw Function URL is useless on its own. Locally (not in Lambda) the check is off.

**Deployment unit**: `template.yaml` (AWS SAM) defines everything — one Lambda function, its Function URL, one DynamoDB table, one S3 bucket, and an IAM role scoped to just those two resources. `CodeUri: app/` means `app/` is flattened to the Lambda root at deploy time, so modules inside `app/` import each other as flat siblings (`import db`, not `from app import db`) — `tests/conftest.py` mirrors this by inserting `app/` onto `sys.path` rather than treating it as a package.

**Auth — single hardcoded account, no registration**:
- `scripts/set_password.py` is a one-off bootstrap: hashes a password with argon2 and writes it, plus a generated JWT signing secret, to SSM Parameter Store (`/yaadi/password_hash`, `/yaadi/jwt_secret`) as `SecureString`s. Re-running it rotates the password *and* the JWT secret, so a password change signs every device out (the warm Lambda keeps the old cached secret until its next cold start).
- `auth.py` issues a 15-minute access JWT and a 30-day refresh JWT on successful login. `get_current_user` (the auth dependency used on every protected router) just validates the access token's signature/expiry/`type` claim — there's no session store or revocation.
- Login has a DynamoDB-backed lockout (`db.record_login_failure`), **per client IP** (`SK = LOGIN_THROTTLE#<ip>`, IP from the Worker-set `X-Client-IP`): 5 consecutive failures from one IP triggers an escalating lockout for that IP (1m → 5m → 30m → 2h → 24h, capped). Per-IP so a stranger can't lock the owner out; trusting `X-Client-IP` is only safe because of the origin-secret check.
- `security.py` caches the SSM-fetched password hash and JWT secret in module-level dicts for the Lambda execution context's lifetime — don't assume every request does a fresh SSM call.

**Data — DynamoDB single-table design** (see `docs/BACKEND_ARCHITECTURE.md` for the full table): one table, `PK = USER#<id>` (hardcoded `USER_ID = "me"` in `db.py`), `SK = <TYPE>#<item_id>` for notes/folders/attachments. `GSI1` (`GSI1PK = PK`, `GSI1SK = UPDATED#<server_write_time>#<item_id>`) exists solely to serve the sync endpoint's "everything changed since timestamp X" query across all item types. Every router module keeps its own `item_to_X` / `X_to_item` mapper functions next to its routes (`notes.py`, `folders.py`) rather than centralizing them in `db.py` or `models.py` — `sync.py` imports those mappers directly rather than duplicating them.

**Deletes are soft** (`deleted_at` set, item kept as a tombstone) so a device that was offline when something was deleted still gets told to delete its local copy on its next pull.

**Sync is the one non-trivial contract** (`sync.py`, mirrored client-side in `yaadi-app`): last-write-wins by `updated_at`, no CRDT/OT. `POST /sync/push` only accepts an incoming item if its `updated_at` is strictly newer than the stored one (one conditional `put_item`, `db.put_if_newer`); otherwise it rejects and hands back the server's copy so the client can reconcile. Invalid items are rejected per item, never a 500 (the client never clears its queue on a failed push), and a future `updated_at` is capped to server now. `GET /sync/pull?since=` walks GSI1SK, which is the *server's* write time, and returns a cursor of `min(max seen, now - 5s)`. Client clocks never touch the cursor, and the 5s lag covers GSI eventual consistency. Re-read the comment in `sync.py::pull` before changing it. All timestamps come from `db.now()` (fixed microsecond precision, compared as strings; the client's `nowIso()` matches that format).

**Attachments never transit the Lambda payload** — `attachments.py` only ever hands out short-lived (5 min) S3 presigned PUT/GET URLs; the actual file bytes go client ↔ S3 directly.

**`links.py` fetches user-supplied URLs** (`GET /links/preview`, og:title/description via stdlib only) — every fetch and every redirect hop goes through `_check_public`, which refuses non-public IPs. Keep that guard on any new outbound fetch (see Finding 7 in `docs/SECURITY_AUDIT.md`).

**`mcp_server.py` is an MCP server at `POST /mcp`** (Claude reads/writes notes through it; see `docs/MCP_SERVER_PLAN.md`). Hand-rolled stateless JSON-RPC, not the `mcp` SDK — the SDK's session manager breaks under Mangum, which runs the ASGI lifespan per invocation. Never name a module `mcp.py` (`app/` is flattened onto `sys.path`, it would shadow the package). Tools call the same `notes.*`/`folders.*` route functions as the REST API, so sync/timestamps/soft deletes stay identical — keep it that way rather than writing to `db` directly. Auth is its own bearer token (SHA-256 in SSM `/yaadi/mcp_token_hash`, read uncached so rotation revokes immediately); PWA JWTs don't open `/mcp` and the MCP token doesn't open the REST API. Notes tagged `private` are invisible to every MCP tool (`_is_private`) — new tools must go through `_visible_notes`/`_get_visible`.

**CORS is handled only by the Function URL's own config in `template.yaml`** (`AllowedOrigins` parameter), not by FastAPI middleware — adding `CORSMiddleware` in `main.py` would double up the `Access-Control-Allow-Origin` header. See the module docstring in `main.py` before touching CORS.

## Security context

`docs/SECURITY_AUDIT.md` is a living self-audit against this app's actual trust model (single principal, so no IDOR/multi-tenant findings — only real boundary violations count). Check it before "fixing" something that's actually an accepted tradeoff for a single-user app, and update it when you fix or introduce a finding.

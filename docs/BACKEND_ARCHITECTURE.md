# Backend Architecture

FastAPI app, deployed as a single AWS Lambda function behind a Lambda Function URL (no API Gateway — see [`INFRA_AND_COST.md`](INFRA_AND_COST.md) for why). One user, one password, no signup flow, no multi-tenant logic.

## Request flow

```
Browser/PWA (yaadi.jaylabs.uk)
      │  HTTPS, fetch('https://api.yaadi.jaylabs.uk/...')  -- cross-origin; CORS answered by the Function URL
      ▼
Cloudflare Worker (yaadi-app/worker.js)   adds X-Origin-Secret + X-Client-IP
      │
      ▼
Lambda Function URL  (main.py 403s anything without X-Origin-Secret)  ──▶  Mangum adapter  ──▶  FastAPI app
                                                   │
                         ┌─────────────────────────┼─────────────────────────┐
                         ▼                         ▼                         ▼
                     DynamoDB                      S3                 SSM Parameter Store
                 (notes/folders)            (attachments)        (password hash, JWT secret)
```

## Repo layout

```
backend/
  app/
    main.py              # FastAPI() instance, Mangum handler, CORS config
    auth.py               # /auth/login, /auth/refresh — JWT issuing/validation
    notes.py               # /notes CRUD
    folders.py              # /folders CRUD
    sync.py                   # /sync/pull, /sync/push — see APP_ARCHITECTURE.md
    attachments.py              # /attachments/presign-upload
    links.py                    # /links/preview — og:title/description for a shared link
    db.py                         # boto3 DynamoDB resource + table helpers
    security.py                    # argon2 hashing, JWT encode/decode, SSM secret cache
    models.py                       # pydantic request/response models
  template.yaml           # AWS SAM template (Lambda, DynamoDB, S3, IAM)
  requirements.txt
```

## Auth

Single hardcoded account — there is no users table, no registration endpoint.

- **Bootstrap**: a one-off script (`scripts/set_password.py`) hashes your chosen password with **argon2** and writes it to SSM Parameter Store as a `SecureString` at `/yaadi/password_hash`. Run once, locally, with your AWS credentials.
- **Login**: `POST /auth/login {password}` → backend reads the hash from SSM (cached in memory for the Lambda's lifetime), verifies with argon2, and on success issues:
  - **access token** — JWT, 15 min expiry, sent in `Authorization: Bearer` header on every request.
  - **refresh token** — JWT, 30 day expiry, used to silently mint new access tokens.
- **JWT signing secret** also lives in SSM (`/yaadi/jwt_secret`), generated once at setup.
- Tokens are stored in the PWA's local storage (not httpOnly cookies) — acceptable here because this is a single-user personal app with no XSS-exposed third-party scripts. Flagged in `README.md` as something to revisit only if this ever becomes multi-user/public.
- No "forgot password" flow. If you forget it, re-run the bootstrap script.

## Why SSM Parameter Store, not Secrets Manager

Secrets Manager charges **$0.40/secret/month**. SSM Parameter Store (Standard tier, `SecureString`) is **free** and does the same job for a value that changes rarely. Given the cost goal, this is the right default — revisit only if you need automatic rotation.

## DynamoDB — single-table design

One table, `yaadi`. Partition key `PK`, sort key `SK`. Partitioning by `USER#<id>` now (even though there's one user) costs nothing extra and means adding a second user later is additive, not a redesign.

| Item type | PK | SK | Notable attributes |
|---|---|---|---|
| Note | `USER#<id>` | `NOTE#<note_id>` | `title`, `content` (markdown string), `folder_id`, `tags[]`, `created_at`, `updated_at`, `deleted_at` (null unless soft-deleted) |
| Folder | `USER#<id>` | `FOLDER#<folder_id>` | `name`, `parent_folder_id`, `created_at` |
| Attachment metadata | `USER#<id>` | `ATTACHMENT#<attachment_id>` | `note_id`, `s3_key`, `content_type`, `size_bytes` |

**GSI1** (`GSI1PK` = `USER#<id>`, `GSI1SK` = `UPDATED#<server_write_time>#<item_id>`) — the server's own clock at write time, never the client's `updated_at`; exists purely to serve the sync endpoint's "give me everything changed since timestamp X" query efficiently, across notes/folders/attachments alike.

Deletes are **soft deletes** (`deleted_at` set, item kept) so that deletions can propagate through sync as tombstones — a device that was offline when you deleted a note still needs to be told to delete its local copy.

## Sync API (the core contract with the client)

See [`APP_ARCHITECTURE.md`](APP_ARCHITECTURE.md) for the client-side half of this. Server side is intentionally simple — last-write-wins by `updated_at`, no operational transforms:

- `GET /sync/pull?since=<cursor>` → all notes/folders *written to the server* after `since` (via GSI1), including soft-deleted tombstones. The returned `synced_at` cursor is a server write time held 5s behind now, so a client clock that runs slow can't make another device skip an item. Treat it as opaque.
- `POST /sync/push` with a batch of locally-changed items → backend writes each one (atomically, via a DynamoDB condition), but **only if** the client's `updated_at` is newer (a future `updated_at` is first capped to server now) than what's already stored (last-write-wins check) — otherwise the server's version wins and is returned back to the client so it can reconcile.

## Attachments

Images/files pasted into a note don't go through Lambda's payload (keeps Lambda cheap and fast):

1. Client asks `POST /attachments/presign-upload {content_type}`.
2. Backend returns a pre-signed S3 PUT URL (and the `s3_key` it generated).
3. Client uploads the file **directly to S3**, then saves the `s3_key` reference on the note.
4. Reading an attachment later: backend returns a pre-signed GET URL on request (short-lived, so the S3 bucket stays private).

## Link previews

`GET /links/preview?url=` returns `{title, description}` read from the page's own link-preview tags (`og:title`/`og:description`, falling back to `<title>`/`meta description`; YouTube goes through its oEmbed endpoint). Stdlib only (`urllib` + `html.parser`), 5 s timeout, first 512 KB. Always 200 — any failure (403, timeout, blocked) returns nulls and the client keeps the bare URL. Every fetch, and every redirect hop, is refused unless the host resolves only to public IPs (so it can't reach the Lambda Runtime API on `127.0.0.1:9001`). See `docs/LINK_SUMMARIES_PLAN.md`.

## Deployment

AWS SAM (`template.yaml`) defines the Lambda function, its Function URL, the DynamoDB table, the S3 bucket, and the IAM role scoped to exactly those two resources. `sam build && sam deploy` is the whole deploy process. Full resource list and cost math in [`INFRA_AND_COST.md`](INFRA_AND_COST.md).

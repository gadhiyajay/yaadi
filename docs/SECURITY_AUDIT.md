# Security Audit — Yaadi Backend

**Date:** 2026-10-06
**Scope:** `backend/app/*.py`, `backend/scripts/set_password.py`, `backend/template.yaml`, `backend/samconfig.toml`, dependency manifests, and `backend/tests/*` (used as executable spec of current behavior). `backend/.venv/` and `backend/.aws-sam/` were excluded (vendored dependencies / build output, not project source).
**Method:** Manual source review against the deployed trust model (single hardcoded user, public Lambda Function URL, JWT bearer auth, DynamoDB single-table store, S3 for attachments). No code was executed against live AWS infrastructure; conclusions about account-level settings (e.g. S3 Block Public Access) that aren't visible in source are marked **Needs validation** rather than assumed.
**Trust model note:** This is a single-user app with no multi-tenant boundary. Several things that would be IDOR/privilege-escalation findings in a multi-user system (e.g. no per-resource ownership checks) are *not* findings here — there is only one principal, so "self access" isn't a boundary crossing. Findings below are limited to things that violate a real boundary: unauthenticated-vs-authenticated, or a control the app/docs claim to provide but doesn't actually hold.

## Summary

| # | Finding | Severity |
|---|---|---|
| 1 | ~~No brute-force protection on `POST /auth/login`~~ — **Fixed** | Medium |
| 2 | ~~Changing the password doesn't end existing sessions~~ — **Fixed** (individual tokens still can't be revoked) | Medium |
| 3 | CORS is wide open (`AllowOrigins: "*"`) by default | Low |
| 4 | ~~S3 attachments bucket has no explicit Public Access Block in the template~~ — **Fixed** | Needs validation |
| 5 | SSM secrets use the default AWS-managed KMS key | Informational |
| 6 | Dependencies are version-pinned with no automated vulnerability scanning | Informational |
| 7 | `GET /links/preview` fetches user-supplied URLs (SSRF surface) — **Mitigated** | Low |
| 8 | ~~Anyone could lock the owner out of `/auth/login` (global lockout counter)~~ — **Fixed** | Medium |

Nothing rated **High** or **Critical** was found. The core auth and data-access logic is sound (see "What's already solid" below).

---

## Finding 1 — No brute-force protection on `POST /auth/login` — Fixed

**Severity:** Medium
**Status:** Fixed — `auth.py` now locks out `/auth/login` after 5 consecutive failures, with an escalating cooldown (1m → 5m → 30m → 2h → 24h) backed by an atomic counter in DynamoDB (`db.record_login_failure`/`clear_login_throttle`). See `tests/test_auth.py::test_login_locks_out_after_repeated_failures` and `::test_successful_login_resets_the_failure_counter`.

**Where:** `backend/app/auth.py:17-24`, `backend/template.yaml` (`FunctionUrlConfig.AuthType: NONE`)

**Issue:** The Lambda Function URL is public and unauthenticated at the AWS layer by design — `/auth/login` has to be reachable by anyone who doesn't yet have a token. The only thing standing between an internet-wide attacker and your notes is the single password checked in `security.verify_password`. There is no rate limiting, no attempt counter, no lockout, and no CAPTCHA. Lambda also auto-scales concurrency, so an attacker isn't limited to one guess at a time the way a single-threaded server would limit them — they can fire many concurrent `/auth/login` requests and get many concurrent argon2 verifications instead of being serialized behind one.

Argon2 (the default `PasswordHasher()` params: ~64 MiB memory, time_cost 2, parallelism 8) makes each *individual* guess expensive, which is real and helpful, but it does not substitute for a lockout/throttle — it only raises the cost-per-guess, not the number of guesses available to the attacker over time.

**Impact:** If the chosen password is anything short of a long random passphrase, this endpoint is the realistic way an opportunistic scanner or targeted attacker gets in. Since this is the one gate in front of all notes and attachments (which the user says may hold passwords/secrets), a successful guess is a full compromise.

**Fix (smallest effective change):**
- Add a per-source (or global, since there's only one legitimate caller) failed-attempt counter with exponential backoff or a hard lockout window — a single DynamoDB item (`PK=USER#me`, `SK=LOGIN_THROTTLE`) with a TTL-based attempt count is enough; no new infra needed.
- Alternatively/additionally, put AWS WAF in front of the Function URL (it supports Function URLs as of 2024) with a rate-based rule on `/auth/login`. This is the lower-code option if you'd rather not touch `auth.py`.
- Make sure the chosen password is high-entropy (`set_password.py` already lets you set anything; the gap is purely the missing throttle, not the hashing).

---

## Finding 2 — Compromised tokens can't be revoked; changing the password doesn't end existing sessions

**Severity:** Medium
**Status:** Partly fixed (2026-10-09) — `set_password.py` now always regenerates `/yaadi/jwt_secret`, so a password change invalidates every outstanding token once the Lambda cold-starts (force it by redeploying or updating the function config). Still open: there's no per-token revocation, and a leaked refresh token stays valid for 30 days unless you rotate.

**Where:** `backend/app/security.py:60-78`, `backend/scripts/set_password.py:40-46`

**Issue:** Access and refresh tokens are stateless JWTs validated only against `JWT_SECRET_PARAM` and their own `exp` claim — there's no server-side session store, `jti`, or revocation list. That means:

- If a refresh token leaks (stolen device, log leak, shoulder-surfed screen, etc.), it's valid for up to **30 days** regardless of anything else you do, *except* rotating the JWT secret.
- Re-running `set_password.py` to change your password does **not** invalidate existing tokens — `set_password.py:40-43` explicitly leaves `JWT_SECRET_PARAM` untouched if it already exists, and token validity has no dependency on the password hash at all. The natural "something's wrong, change my password" reaction doesn't actually lock anyone out.
- Refresh tokens can also be used repeatedly to mint new access tokens indefinitely until they expire — there's no rotation or single-use/reuse-detection, so a leaked refresh token is a standing 30-day key, not a one-time risk.

**Impact:** Medium rather than High because it requires a token to already be stolen (it's not an auth bypass by itself), but the blast radius is the same as a full compromise once that happens, for up to 30 days, with no way to cut it short.

**Fix (smallest effective change):**
- Give `set_password.py` a flag (or just always do it) to regenerate `JWT_SECRET_PARAM` on every password change — rotating the secret immediately invalidates every outstanding token. This is a ~3 line change (`Overwrite=True` instead of the current "leave if exists" branch) and needs no new infra.
- Optionally shorten the refresh token lifetime if 30 days feels too long for the sensitivity of the data.

---

## Finding 3 — CORS is wide open (`AllowOrigins: "*"`) by default

**Severity:** Low

**Where:** `backend/template.yaml:8-12, 30-38, 80-85`

**Issue:** `AllowedOrigins` defaults to `"*"` for both the Function URL CORS config and the S3 bucket CORS config, and `AllowMethods`/`AllowHeaders` are also `"*"`. Because auth is bearer-token-in-header (not a cookie) and `AllowCredentials` isn't set, this isn't directly exploitable the way wildcard CORS + cookies would be — a malicious page can't make the victim's browser silently attach a valid token it doesn't have. So this isn't a confirmed boundary violation today.

**Impact:** Low — it's a defense-in-depth gap, not a demonstrated exploit path. It does mean any website can hit `/health` and `/auth/login` cross-origin (login attempts would still need the real password), and it removes one layer of protection if the frontend's token-storage model ever changes.

**Update:** the app now calls the API at `api.yaadi.jaylabs.uk` (a Cloudflare Worker that adds a secret header), and the Function URL rejects requests without that secret, so cross-origin calls straight to the Function URL get nothing useful regardless of CORS. `samconfig.toml` already restricts `AllowedOrigins` to the app and localhost dev origins.

**Fix:** Set `AllowedOrigins` to your actual frontend domain(s) in `samconfig.toml`'s `parameter_overrides` for production deploys instead of relying on the `"*"` default. Keep `"*"` only for local/dev convenience if needed.

---

## Finding 4 — S3 attachments bucket has no explicit Public Access Block (Needs validation)

**Severity:** Needs validation (no severity assigned — see methodology)
**Status:** Fixed (2026-10-09) — `template.yaml` now sets all four `PublicAccessBlockConfiguration` flags on `AttachmentsBucket`, so this no longer depends on account defaults.

**Where:** `backend/template.yaml:76-85`

**Issue:** `AttachmentsBucket` doesn't declare a `PublicAccessBlockConfiguration`, and the template attaches no bucket policy. New buckets on accounts created after April 2023 default to all four S3 Block Public Access settings = true at the account level, which would make this moot — but that's an account-level setting not visible in this repo, and older accounts or accounts where that default was changed wouldn't be covered.

**What's needed to resolve:** Confirm (in the AWS console or via `aws s3api get-public-access-block --bucket <name>` and `aws s3control get-public-access-block --account-id <id>`) that both the bucket and the account have Block Public Access fully enabled. If you want this enforced in code rather than relying on account defaults, that's the fix.

**Fix (if validation finds a gap):** Add an explicit block to `template.yaml`:
```yaml
  AttachmentsBucket:
    Type: AWS::S3::Bucket
    Properties:
      ...
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
```

---

## Finding 5 — SSM secrets use the default AWS-managed KMS key

**Severity:** Informational

**Where:** `backend/scripts/set_password.py:35-45`

**Issue:** `put_parameter(..., Type="SecureString", ...)` with no `KeyId` uses the account's default `alias/aws/ssm` key. This is still strong encryption at rest and access is already scoped correctly via `SSMParameterReadPolicy` in `template.yaml` — this is a hardening note, not a vulnerability. A customer-managed key would add a separate key-policy layer and audit trail, which only matters if you want finer-grained control over who/what can decrypt (irrelevant for a single-user hobby project unless you want it for its own sake).

**Fix (optional):** Not necessary at this scale; skip unless you want the extra control for its own sake.

---

## Finding 6 — Dependencies are pinned with no automated vulnerability scanning

**Severity:** Informational

**Where:** `backend/app/requirements.txt`, `backend/requirements-dev.txt`

**Issue:** `fastapi==0.115.0`, `mangum==0.17.0`, `boto3==1.34.162`, `pydantic==2.9.2`, `pyjwt==2.9.0`, `argon2-cffi==23.1.0` are all exact-pinned (good for reproducibility) but there's no Dependabot/`pip-audit`/CI check to flag a future CVE in one of them. Nothing in the current pinned set has a known issue reviewed here, and the auth-relevant usage (`jwt.decode(..., algorithms=["HS256"])` with an explicit algorithm list) is already written in the safe pattern that avoids the classic PyJWT "algorithm confusion" class of bug.

**Fix (optional, low effort):** Turn on GitHub's Dependabot security updates for this repo (free, zero config beyond enabling it) once the repo is pushed to GitHub.

---

## Finding 7 — `GET /links/preview` fetches user-supplied URLs — Mitigated

**Where:** `backend/app/links.py`

The endpoint makes an outbound HTTP request to whatever URL the client sends, which is a classic SSRF shape. Inside Lambda the interesting internal target is the Runtime API on `127.0.0.1:9001` (`AWS_LAMBDA_RUNTIME_API`); the function isn't in a VPC and Lambda has no instance-metadata endpoint, so there's little else to reach.

**Mitigation in place:** `_check_public` resolves the host and refuses unless *every* address is `ipaddress.ip_address(...).is_global` (blocks loopback, RFC 1918, link-local `169.254.0.0/16`, IPv4-mapped IPv6, etc.) and only allows `http`/`https`. `_CheckedRedirect` re-runs the same check on every redirect hop, so an open redirect on a public site can't bounce the request inward. The endpoint requires a valid access token, sends no credentials of its own, returns only `og:`/`<title>` text (never raw response bodies), and reads at most 512 KB with a 5 s timeout. Covered by `tests/test_links.py`.

**Accepted residual risk:** DNS rebinding — the host is resolved once for the check and again by `urllib` when connecting, so a hostile DNS server could answer differently the second time. Exploiting it requires a valid access token, i.e. the single principal attacking themselves; pinning the connection to the checked IP isn't worth the code at this trust level.

---

## Finding 8 — Anyone could lock the owner out of `/auth/login` — Fixed

**Severity:** Medium (availability)

**Where:** `backend/app/auth.py`, `backend/app/db.py`, `backend/app/main.py`, `yaadi-app/worker.js`

**Issue:** The Finding 1 lockout used a single global counter. Anyone who knew the Function URL (it was compiled into the public JS bundle) could send 5 wrong passwords and lock the owner out, escalating to 24 h lockouts indefinitely. Already-logged-in devices kept working until their 30-day refresh token expired, but no new login was possible.

**Fix:** 
- The lockout is now keyed per client IP (`LOGIN_THROTTLE#<ip>`), so a guesser only locks out their own IP.
- The IP has to be trustworthy for that to mean anything. All traffic now goes through a Cloudflare Worker that sets `X-Client-IP` from Cloudflare's `CF-Connecting-IP` (overwriting anything the caller sent) and adds `X-Origin-Secret`. In Lambda, a middleware in `main.py` 403s every request without the secret (constant-time compare), so the raw Function URL can't be used to spoof `X-Client-IP` or bypass Cloudflare.
- Covered by `test_lockout_is_per_client_ip` and `tests/test_main.py`.

**Accepted tradeoffs:**
- A distributed guesser now gets 5 attempts per IP per escalation step instead of 5 total. With argon2 and a strong password this is still impractical; add a Cloudflare rate-limiting rule on `api.yaadi.jaylabs.uk/auth/login` if that ever changes.
- Rejected requests still invoke the Lambda for a few milliseconds (~$0.40 per million), so a flood against the raw Function URL costs a little money but can't reach any logic.
- Throttle items for IPs that never log in successfully are never deleted (tiny; add a DynamoDB TTL if needed).
- Rotating the origin secret needs both sides updated: SSM `/yaadi/origin_secret` and `wrangler secret put ORIGIN_SECRET`. Warm Lambdas keep the old SSM value until their next cold start.

---

## What's already solid

Worth stating explicitly so future changes don't accidentally regress these:

- **JWT algorithm is pinned explicitly** (`security.py:75`, `algorithms=[JWT_ALGORITHM]`) — not vulnerable to the `alg: none` / algorithm-confusion class of attack.
- **Every data-mutating router requires a valid access token** (`notes.py:11`, `folders.py:11`, `sync.py:22`, `attachments.py:17-19` all carry `dependencies=[Depends(get_current_user)]`); only `/auth/login`, `/auth/refresh`, and `/health` are intentionally public. Verified against `test_notes_requires_auth` in `tests/test_auth.py`.
- **No injection surface**: DynamoDB access goes through parametrized `boto3.dynamodb.conditions.Key` expressions everywhere (`db.py`), never string-built queries.
- **No secrets committed to the repo** — password hash and JWT secret live only in SSM `SecureString` parameters, fetched at runtime; the only password-like string in source is the test fixture's dummy password in `tests/conftest.py`. `.venv/` and `.aws-sam/` (which could otherwise leak build artifacts) are correctly gitignored.
- **Attachments never transit the Lambda payload** and presigned URLs are short-lived (5 minutes), limiting exposure if a URL is logged somewhere.
- **IAM policies are scoped to this app's own table/bucket** via SAM's `DynamoDBCrudPolicy`/`S3CrudPolicy`/`SSMParameterReadPolicy` managed policy templates — no wildcard or admin-level permissions.

---

## Priority order if you action this

1. **Finding 1** (login throttling) — cheapest to fix, highest real-world value since it's the one gate protecting everything.
2. **Finding 2** (rotate JWT secret on password change) — a ~3-line fix that makes "change your password" actually mean what it sounds like.
3. **Finding 4** (confirm S3 Block Public Access) — a 2-minute AWS console check, zero code.
4. Findings 3, 5, 6 — cheap hardening, do whenever convenient; none are urgent at this scale.

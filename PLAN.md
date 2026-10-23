# PLAN: Vault (personal and family details behind a second lock)

Status: **proposal, nothing built yet.** The frontend half lives in `yaadi-app/PLAN.md`; read both.
Date: 2026-10-23

## 1. What we're building

A **Vault** inside Yaadi for the most sensitive things: Aadhaar, PAN, passport, driving licence,
voter ID, bank accounts, cards, insurance policies, medical details, logins and passwords, for
Jay and for each family member. It sits behind a **second lock** on top of the normal Yaadi
login, and the server can never read what's in it.

Two independent layers, on purpose:

| Layer | What it stops | How |
|---|---|---|
| **Gate** (server-side second factor) | Someone holding a logged-in device or a stolen 30-day refresh token from even *downloading* the vault | Every `/vault/*` data route needs a short-lived **vault token**, issued only after a **passkey** check (fingerprint / face / device PIN), or a TOTP code as backup |
| **Seal** (client-side encryption) | Anyone with the database: an AWS account compromise, a leaked backup, a bug in this API, Claude via MCP | Every vault item is encrypted in the browser with AES-256-GCM before it leaves the device. The key comes from a **vault passphrase** (or the passkey itself, see PRF below) and never reaches the server |

So: passing the gate gets you ciphertext; you still need the key to read it. Losing the
database leaks nothing readable.

## 2. Threat model (what this does and doesn't defend)

Defends against:
- **Stolen or borrowed unlocked phone/laptop** where Yaadi is logged in: the gate needs a
  fresh biometric/PIN on *that* device's passkey, and the seal needs the passphrase.
- **Stolen tokens** (`localStorage` holds the 30-day refresh token, see SECURITY_AUDIT Finding 2):
  tokens alone open notes, not the vault.
- **Server-side exposure**: DynamoDB dump, AWS console access, logs, this Lambda being buggy or
  compromised *after the fact*, MCP/Claude. All of them only ever see ciphertext.
- **Online guessing**: the gate is rate-limited per IP and (for TOTP) globally; the passphrase is
  never checked by the server at all, so there's nothing to guess against online.

Does **not** defend against (stated so nobody assumes otherwise):
- **Malicious JavaScript running in the app while the vault is unlocked** (XSS or a compromised
  frontend deploy). Decryption happens in the page. Mitigation: strict CSP (frontend plan,
  phase 0), no third-party scripts, keep `markdown-it` `html:false`.
- **Offline brute force of a weak passphrase** by someone who already has the database.
  Mitigation: Argon2id key derivation plus a minimum strength rule.
- **Forgetting the passphrase *and* losing the recovery key *and* every PRF passkey.** The data is
  then unrecoverable, by design. There is no server-side backdoor.
- Metadata: the server can see how many vault items exist, their sizes (padded) and when they
  changed, but not their type, owner or contents.

## 3. Which "auth service" (free)

Options for the second layer, and the pick:

| Option | Cost | Verdict |
|---|---|---|
| **Passkeys (WebAuthn)**: Google Password Manager on Android/Chrome, Windows Hello, iCloud Keychain, or a hardware key | Free, no vendor account | **Primary.** Biometric, can't be phished, an open standard; the "service" is the passkey manager already on Jay's devices. Library: `webauthn` (py_webauthn, Duo Labs, BSD) |
| **TOTP** (Google Authenticator, Aegis, Microsoft Authenticator, 2FAS) | Free | **Backup**, for when no passkey device is to hand. Library: `pyotp` (MIT) |
| **Recovery codes** (10 one-time codes, printed) | Free | **Last resort** for the gate |
| Hosted identity (AWS Cognito, Auth0, Clerk, Firebase Auth) | Free tiers exist | **Not chosen.** It's another account and SDK, and their script would run on the very page that decrypts Aadhaar numbers. They only *gate* and don't encrypt, and their MFA is passkeys or TOTP underneath anyway. Cognito SMS MFA costs money |
| SMS / email OTP | SMS costs money; email is free-ish | **Not chosen.** SIM-swap and inbox compromise are the usual ways these get broken |

**Bonus: passkey PRF ("fingerprint unlock").** The WebAuthn `prf` extension lets a passkey
produce a stable secret. Where the passkey manager supports it (Google Password Manager on
Android/Chrome does; Windows Hello support varies, so this is detected at runtime), that secret
also wraps the vault key, so one fingerprint both passes the gate *and* decrypts. The
passphrase always works as a fallback.

## 4. Key design (summary; client does all of this, server stores blobs)

- One random 256-bit **vault key** (DEK). Items are encrypted with it.
- The DEK is stored only **wrapped** (AES-GCM) in **key slots**, like LUKS disk encryption:
  - `passphrase` slot: wrapping key = Argon2id(passphrase, salt; m=64 MiB, t=3, p=1)
  - `recovery` slot: wrapping key from a 128-bit printed recovery key
  - `prf` slot, one per PRF-capable passkey: wrapping key = HKDF(PRF output)
- Item ciphertext: AES-256-GCM, random 96-bit IV, **AAD = item id + format version** (so a blob
  can't be moved onto another item's id), plaintext padded to a multiple of 256 bytes.
- Change passphrase = rewrap the DEK in one slot (instant). "Rotate vault key" = re-encrypt all items (rare).

The server never receives the passphrase, the recovery key, the PRF output, the DEK, or any
plaintext.

## 5. Data model (DynamoDB, same single table)

All under `PK = USER#me`. **None of these items get `GSI1PK`/`GSI1SK`**, so `GET /sync/pull`
can't return them by construction (it only walks GSI1).

| SK | Holds | Notes |
|---|---|---|
| `VAULT#META` | `rev`, `kdf {alg, m, t, p, salt}`, `slots [{id, kind, credential_id?, iv, wrapped}]`, `check {iv, ct}` (encrypts a known string so a wrong passphrase is detected locally), `created_at`, `updated_at` | Written only with `ConditionExpression rev = :expected` so two devices can't clobber each other's slots |
| `VAULT#ITEM#<uuid>` | `id`, `ct` (base64), `iv`, `v` (format version), `size`, `updated_at`, `deleted_at` | Opaque. People, entries and document metadata are all just items whose decrypted JSON says what they are. Last-write-wins via existing `db.put_if_newer` |
| `VAULT#CRED#<credential_id>` | `public_key`, `sign_count`, `transports`, `name` ("Pixel 9", "Windows Hello"), `prf` (bool), `created_at`, `last_used_at` | Passkeys allowed to open the gate |
| `VAULT#TOTP` | `last_step` (replay guard) | The TOTP secret itself is an SSM SecureString, `/yaadi/vault_totp_secret`, written by `scripts/vault_totp.py` and read uncached (like the MCP token) so re-running the script revokes the old one at once |
| `VAULT#RECOVERY#<sha256>` | `used_at` | 10 one-time gate recovery codes, stored hashed |
| `VAULT#CHALLENGE#<id>` | `challenge`, `purpose`, `ttl` | WebAuthn challenges: single-use (conditional delete), 2-minute expiry |
| `VAULT_THROTTLE#<ip>`, `VAULT_THROTTLE#totp` | failure counters | Same escalating lockout as login, stricter; TOTP also has a global counter |
| `VAULT#AUDIT#<ts>#<id>` | `event` (unlock_ok, unlock_fail, passkey_added, ...), `ip`, `user_agent`, `ttl` (90 days) | Shown in the vault UI as "Recent unlocks" |
| `VAULT#SETUP_USED#<sha256>` | `used_at` | Makes the setup code single-use without the Lambda needing SSM write access |

Document scans (phase 4): encrypted in the browser, uploaded through presigned URLs under
`s3://…/me/vault/<uuid>` exactly like attachments today; the bucket only ever holds ciphertext.

## 6. API

All routes also require the normal access token (`get_current_user`). The MCP token never opens
`/vault/*`, and a vault token never opens anything else.

**Gate (access token only)**

| Route | Does |
|---|---|
| `GET /vault/status` | `{setup, passkeys: n, totp, locked_until}`. Lets the UI pick setup or unlock |
| `POST /vault/setup/begin` `{setup_code}` | Only while no passkey exists. Checks the code against SSM `/yaadi/vault_setup_code_hash` (written by `scripts/vault_setup_code.py`, with an expiry) and returns registration options with `prf` requested |
| `POST /vault/setup/finish` `{credential}` | Verifies the attestation, stores the credential, marks the setup code used, returns a vault token |
| `POST /vault/unlock/begin` | Authentication options (challenge, `allowCredentials`, PRF eval salt) |
| `POST /vault/unlock/finish` `{assertion}` | Verifies the signature, origin, RP ID, user-verification flag and sign counter, then returns `{vault_token, expires_at}` |
| `POST /vault/unlock/totp` `{code}` | Same result through TOTP (±1 step, no reuse of the last step) |
| `POST /vault/unlock/recovery` `{code}` | Same result through a one-time recovery code |

**Vault (access token + `X-Vault-Token`)**

| Route | Does |
|---|---|
| `GET /vault/meta`, `PUT /vault/meta` `{rev, …}` | Key slots and KDF params; the PUT is conditional on `rev` |
| `GET /vault/items` | Every item including tombstones (expected scale is hundreds, so no paging cursor needed; add `?since=` later if it ever matters) |
| `PUT /vault/items/{id}` | LWW upsert; a rejection returns the server copy (same contract as `/sync/push`) |
| `GET /vault/passkeys`, `POST /vault/passkeys/begin\|finish`, `DELETE /vault/passkeys/{id}` | Manage passkeys. Adding one needs a vault token that is **< 2 minutes old** (fresh re-auth). Deleting the last one is refused |
| `POST /vault/recovery-codes` | Regenerate the 10 gate recovery codes (the old ones die) |
| `GET /vault/audit` | Last 50 events |
| `POST /vault/session/extend` | New vault token; capped at 60 min after the original unlock (`auth_time` claim) |
| `POST /vault/files/presign-upload`, `GET /vault/files/{id}/download-url` | Phase 4 |

**Vault token**: HS256 JWT with `type: "vault"`, 10-minute expiry, `auth_time`, and `amr`
(`passkey` / `totp` / `recovery`), signed with a **separate** secret (SSM `/yaadi/vault_jwt_secret`).
`decode_token` already rejects a wrong `type`, so access tokens can't stand in for vault tokens.
Sent as `X-Vault-Token`, not `Authorization`, so both tokens travel together.

## 7. Bootstrap and recovery (who's allowed to register a passkey?)

This is the subtle part. If any logged-in session could register a passkey, a stolen session
could enrol its own and walk past the gate. So:

1. **First passkey** needs a **setup code** that only someone with AWS access can make:
   `python scripts/vault_setup_code.py` prints a 10-word code and writes its SHA-256 plus a
   30-minute expiry to SSM. The code is single-use.
2. **More passkeys** (the laptop, a second phone) need an existing vault session that is less than
   2 minutes old.
3. **Lost every passkey?** Use TOTP or a recovery code to get through the gate, then enrol a new
   passkey. **Lost those too?** Run `scripts/vault_reset_gate.py`, which deletes the
   `VAULT#CRED#*`/`VAULT#TOTP` items (the encrypted data stays untouched), then run the setup
   code script again. This needs AWS credentials, which is the real root of trust here.
4. **Forgot the passphrase?** The recovery key or a PRF passkey unwraps the DEK, and the UI then
   asks for a new passphrase. No passphrase, recovery key or PRF passkey left means the data is gone.

## 8. Files to add or change

| File | Change |
|---|---|
| `backend/app/vault.py` (new) | Router: everything in §6. Mappers live next to the routes, as in `notes.py` |
| `backend/app/vault_auth.py` (new) | WebAuthn options and verification (py_webauthn), TOTP, recovery codes, vault-token issue/verify, the `require_vault_token` dependency, and the throttle |
| `backend/app/models.py` | `VaultItem`, `VaultMeta`, `VaultSlot`, `VaultTokenResponse`, WebAuthn request/response shapes |
| `backend/app/db.py` | SK helpers (`vault_item_sk` …), `put_if_rev` (conditional on `rev`), `delete_once` (single-use challenge), a generic `record_failure(key, …)` extracted from `record_login_failure` (keep the old name as a thin wrapper) |
| `backend/app/security.py` | `get_vault_jwt_secret()`, `create_vault_token(amr)`, `decode_vault_token()` |
| `backend/app/main.py` | `app.include_router(vault.router)` |
| `backend/app/requirements.txt` | `webauthn==<pin at build time>`, `pyotp==<pin>` (both pure Python apart from `cryptography`, which ships manylinux wheels; if `sam build` on Windows grabs the wrong wheel, use `sam build --use-container`) |
| `backend/template.yaml` | `TimeToLiveSpecification: {AttributeName: ttl, Enabled: true}` on the table (challenges, audit); env vars `VAULT_RP_ID=yaadi.jaylabs.uk`, `VAULT_ORIGINS=https://yaadi.jaylabs.uk` (plus `http://localhost:5173` locally, where RP ID is `localhost`). No new IAM: the existing `SSMParameterReadPolicy` on `yaadi/*` covers the new parameters |
| `backend/scripts/vault_setup_code.py`, `vault_reset_gate.py`, `vault_totp.py` (new) | §7; `vault_totp.py` makes a TOTP secret, prints the `otpauth://` URI as a terminal QR code for your authenticator app, and writes it to SSM |
| `backend/scripts/set_password.py` | Also rotate `/yaadi/vault_jwt_secret` |
| `backend/tests/test_vault.py` (new) | §9 |
| `docs/SECURITY_AUDIT.md` | New Finding 10, "Vault", covering the model, mitigations and accepted residual risks (§2) |
| `docs/BACKEND_ARCHITECTURE.md`, `CLAUDE.md` | New SK rows, the "vault items never get GSI1 keys" rule, MCP never touches vault, the vault-token contract |

`mcp_server.py` needs **no change**. It only calls `notes.*`/`folders.*`; a test pins that.

## 9. Tests (moto, like the rest)

- Every `/vault/*` data route returns 401 without a vault token, and also 401 with an access
  token placed in `X-Vault-Token`. A vault token in `Authorization` doesn't open notes.
- The MCP token gets 401 on `/vault/*`; no MCP tool can list or read a `VAULT#` item.
- `/sync/pull` never returns vault items, even right after they change (no GSI1 keys).
- Setup: a wrong code, an expired code, a reused code and a second setup once a passkey exists
  are all refused.
- WebAuthn: a challenge works once (a replay fails), a wrong origin or RP ID fails, a missing
  user-verification flag fails, and a sign-counter regression fails. Use py_webauthn's own test
  vectors or a software-authenticator fixture, and mock nothing inside the verification itself.
- TOTP: ±1 step accepted, the same step reused is refused, and the global failure limit kicks in.
- Throttle: 5 bad unlocks from one IP lock that IP only.
- Items: LWW put, a stale put returns the server copy, tombstones are returned, and an oversized
  item (> 350 KB) gets a 413 instead of a DynamoDB 500.
- Meta: a PUT with a stale `rev` gets a 409.
- Passkeys: adding one with a vault token older than 2 minutes is refused, and deleting the last
  one is refused.

## 10. Phases

| Phase | Backend | Ships with frontend phase |
|---|---|---|
| 0 | TTL on the table, `record_failure` refactor (no behaviour change, existing tests stay green) | CSP in `worker.js` |
| 1 | Gate (setup code, passkeys), vault token, meta and items, audit, tests | Setup wizard, unlock, Vault UI with people and entries |
| 2 | TOTP (script + unlock route), recovery codes, passkey management, session extend | Security settings screen |
| 3 | (none) | PRF fingerprint unlock, expiry reminders, encrypted export |
| 4 | Vault file presign routes | Encrypted document scans |
| 5 | (none) | Optional encrypted offline cache; Android app passkeys (needs `/.well-known/assetlinks.json` on yaadi.jaylabs.uk and Credential Manager in the Capacitor shell) |

Cost: everything stays inside the existing always-free tiers (Lambda, DynamoDB, SSM standard
parameters). Passkeys and TOTP apps are free. **Expected cost: $0.**

## 11. Open questions (defaults in bold; say if you want otherwise)

1. Should the vault passphrase be separate from the Yaadi login password? **Yes.** Reusing it
   would let anyone who knows the login password read the vault.
2. Fingerprint-only unlock (PRF) wherever the device supports it? **Yes, with the passphrase as fallback.**
3. Auto-lock: **5 min idle, or 60 s after the app goes to the background.**
4. Offline vault access? **Not in v1** (online gate only); an opt-in encrypted offline cache comes in phase 5.
5. Unlock alerts (a Telegram message, or an email via SES, on every unlock or failed attempt)?
   **Off by default**; it's easy to add on top of the audit log.
6. TOTP enrolment: **a local script** (`scripts/vault_totp.py`), which matches how every other
   secret here is set and needs no new IAM. The alternative, enrolling from inside the app, needs
   the Lambda to write secrets (KMS/SSM write permissions), which is more power than it has today.

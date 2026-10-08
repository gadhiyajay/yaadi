# Yaadi — Backend

FastAPI backend for [Yaadi](https://github.com/gadhiyajay/yaadi-app), a single-user, Obsidian/Notion-flavored note app built to work offline-first with near-zero hosting cost. This repo is the API; the PWA frontend lives in [`yaadi-app`](https://github.com/gadhiyajay/yaadi-app).

## Decisions locked in

| Question | Decision | Why |
|---|---|---|
| Offline support | **Offline-first with sync** | The client treats the local copy as the source of truth and syncs in the background. |
| App delivery | **PWA** (installable web app) | One React codebase → phone + laptop, no app store fees, free hosting. |
| Note format | **Markdown documents** | Simple schema, trivially exportable/backup-able, matches Obsidian mental model. |
| Cloud | **AWS** (Lambda + DynamoDB) | Both have a *perpetual* free tier (not just 12 months) — realistically $0/mo for one user. |
| Auth | **Fixed single-user password login** | No signup flow, no multi-tenant complexity — just you. |

## Tech stack at a glance

- **Backend**: FastAPI, wrapped for Lambda via Mangum.
- **Database**: DynamoDB (single-table design).
- **File storage**: S3 (for pasted images/attachments).
- **Auth**: single-user password login → JWT access + refresh tokens. No OAuth, no user table.
- **Infra**: AWS Lambda (Function URL, not API Gateway), DynamoDB, S3, SSM Parameter Store — deployed via AWS SAM.

## Docs

- [`docs/BACKEND_ARCHITECTURE.md`](docs/BACKEND_ARCHITECTURE.md) — FastAPI on Lambda, DynamoDB schema, auth, sync API, attachments.
- [`docs/INFRA_AND_COST.md`](docs/INFRA_AND_COST.md) — exact AWS resources, why Lambda over EC2/GCP, cost table, deploy tooling.
- [`docs/SECURITY_AUDIT.md`](docs/SECURITY_AUDIT.md) — a self-audit of the auth/trust model against this single-user design. Read this before deploying your own copy with real data in it.
- Frontend/client half of the sync contract: [`yaadi-app`'s README](https://github.com/gadhiyajay/yaadi-app).

## Running locally

Requires Python 3.11+.

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate   # or `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt -r requirements-dev.txt
```

### Tests

Tests mock all AWS calls via [`moto`](https://github.com/getmoto/moto) — no AWS account or credentials needed to run them.

```bash
cd backend
pytest                      # whole suite
pytest tests/test_auth.py   # one file
pytest tests/test_auth.py::test_login_locks_out_after_repeated_failures  # one test
```

## Deploying your own instance

This deploys a Lambda function, a DynamoDB table, an S3 bucket, and an IAM role scoped to just those two resources — nothing shared with anyone else's deployment.

1. Install the [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) and configure AWS credentials (`aws configure`) for an account you control.
2. Edit `backend/samconfig.toml` — change `stack_name`, `region`, and `AllowedOrigins` (your own frontend's URL, not `yaadi.jaylabs.uk`) to your own values.
3. Bootstrap your password and JWT signing secret (writes to SSM Parameter Store, run once with your AWS credentials):
   ```bash
   python backend/scripts/set_password.py
   ```
4. Build and deploy:
   ```bash
   ./backend/scripts/deploy.ps1   # or: sam build && sam deploy, from backend/
   ```
5. Note the `FunctionUrl` output and point the frontend's `VITE_API_BASE_URL` at it (see `yaadi-app`'s README).

No secrets are stored in this repo or in CloudFormation parameters in plaintext — the password hash and JWT secret live only in SSM `SecureString` parameters.

## Security model

This is a single-user hobby app, not a multi-tenant SaaS — see the "Trust model note" at the top of [`docs/SECURITY_AUDIT.md`](docs/SECURITY_AUDIT.md) for what that does and doesn't mean for findings there. If you find a new issue, please open an issue rather than a PR with the exploit details for anything that could affect a live deployment (there isn't a shared hosted instance, so this mostly matters for your own fork).

## Open items / future enhancements (deliberately deferred, not forgotten)

- Full CRDT-based conflict resolution — using simple last-write-wins for now since it's one person on 1-2 devices; real concurrent-edit conflicts are rare. Revisit only if it ever causes a real lost edit.
- Multi-user support — the DynamoDB schema is already partitioned by `USER#<id>` so this wouldn't require a redesign, just an added signup flow.
- Server-side full-text search (e.g. OpenSearch) — not needed because offline-first means every note already lives on-device, so search runs client-side for free.
- Custom domain — optional, not required to use the app.
- Token revocation on password change, and rate-limiting hardening — see open findings in `docs/SECURITY_AUDIT.md`.

## Contributing

This started as a personal project but issues and PRs are welcome — especially for the hardening items above. Please run `pytest` before opening a PR.

## License

[MIT](LICENSE)

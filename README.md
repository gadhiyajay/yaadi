# Yaadi: backend

**Yaadi** is a personal, offline-first notes app for one person, on phone and laptop. Notes are written in Markdown, sync between devices, and work without internet. It runs on AWS for about **$0/month**.

This repo is **only the backend** (the API). There's no frontend here on purpose. Instead, [`prompt.md`](prompt.md) describes exactly what a frontend needs to do, so you can paste it into an AI coding assistant and have it build one for you.

---

## How it fits together

```
 Your app (PWA, in the browser)
        │
        ▼
 Cloudflare Worker  ── adds a secret header + the caller's IP
        │
        ▼
 AWS Lambda (this repo: FastAPI)
        │
        ├── DynamoDB          notes & folders
        ├── S3                images (uploaded/downloaded directly by the app)
        └── SSM Param Store   password hash, JWT secret, other secrets
```

- **Single user.** One password and no sign-up. Nobody else can create an account.
- **Offline-first.** The app keeps everything locally and syncs in the background. If two devices edit the same note, the most recent edit wins.
- **Cheap.** Lambda, DynamoDB and S3 all have free tiers that one person won't outgrow.
- **Optional AI access.** Claude (or another MCP client) can read and write your notes through a built-in MCP server.

## What you need

- An **AWS account** + the [AWS CLI](https://aws.amazon.com/cli/) (run `aws configure` once)
- The [**AWS SAM CLI**](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
- **Python 3.11+**
- A **Cloudflare account** with a domain on it (the free plan is fine), used for the Worker that sits in front of the API

## Set it up (about 20 minutes)

### 1. Get the code and install

```bash
git clone https://github.com/gadhiyajay/yaadi.git
cd yaadi/backend
python -m venv .venv
.venv/Scripts/activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Optional: run `pytest` to check everything works. The tests fake AWS, so they don't need an account.

### 2. Make it yours

Open `backend/samconfig.toml` and change:

- `region`: the AWS region you want (use the same one everywhere below)
- `stack_name`: any name you like
- `AllowedOrigins`: **your** frontend's URL, e.g. `https://notes.your-domain.com` (you can keep `http://localhost:5173` for local development)

### 3. Set your password

```bash
python scripts/set_password.py
```

This asks for a password, stores a secure hash of it in AWS (it never stores the password itself), and creates the secret used to sign login tokens. Run it again any time to change your password. That also signs out every device.

### 4. Create the origin secret

The backend only answers requests that come through your Cloudflare Worker. The two share a secret. Generate one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Save it in AWS (replace `<secret>` with the value you just generated):

```bash
aws ssm put-parameter --name /yaadi/origin_secret --type SecureString --value "<secret>"
```

Keep this value handy. You'll give the **exact same** one to the Worker in step 6.

### 5. Deploy

```bash
sam build && sam deploy
# Windows alternative: ./scripts/deploy.ps1
```

When it finishes, copy the **`FunctionUrl`** from the output. It looks like `https://abc123….lambda-url.us-east-1.on.aws/`.

### 6. Build the frontend + Worker

Paste [`prompt.md`](prompt.md) into your AI coding assistant in a new folder. Section 9 of it covers the Worker. You'll need to:

- Put your `FunctionUrl` (without the trailing `/`) in the Worker's `API_ORIGIN`.
- Run `npx wrangler secret put ORIGIN_SECRET` and paste the secret from step 4.
- Point the frontend's `VITE_API_BASE_URL` at your Worker's API domain (e.g. `https://api.your-domain.com`).

**Every request returns `403 Forbidden`?** The secret in the Worker doesn't exactly match the one in AWS. The usual cause is a stray newline, often added when piping the value in PowerShell. Paste it in interactively instead. If you changed the value in AWS, the running Lambda may still have the old one cached for a while. Redeploying makes it pick up the new one right away.

### 7. Optional: let Claude use your notes

```bash
python scripts/set_mcp_token.py
```

This prints a token and the `claude mcp add` command to run. The command it prints uses the original author's domain, so replace that URL with your own `https://api.your-domain.com/mcp`. Run the script again to replace the token (the old one stops working immediately). Notes tagged `private` are never visible to Claude. More details are in [`docs/MCP_SERVER_PLAN.md`](docs/MCP_SERVER_PLAN.md).

## The API at a glance

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `POST /auth/refresh` |
| Sync | `POST /sync/push`, `GET /sync/pull?since=` |
| Notes / folders | `GET POST /notes`, `GET PATCH DELETE /notes/{id}` (same for `/folders`) |
| Images | `POST /attachments/presign-upload`, `GET /attachments/{id}/download-url` |
| Link previews | `GET /links/preview?url=` |
| AI (MCP) | `POST /mcp` |
| Health | `GET /health` |

The full request/response details are in [`prompt.md`](prompt.md).

## Security, in short

- No secrets live in this repo. Everything sensitive is in AWS SSM as encrypted `SecureString`s.
- Passwords are hashed with argon2. Login tokens expire after 15 minutes (refresh tokens after 30 days).
- 5 wrong passwords from one IP lock that IP out, starting at 1 minute and growing up to 24 hours. Other IPs (like yours) aren't affected.
- Images never pass through the API. The app gets short-lived (5-minute) S3 links instead.
- The link-preview fetcher refuses to reach private or internal network addresses.

[`docs/SECURITY_AUDIT.md`](docs/SECURITY_AUDIT.md) is an honest self-audit, including the trade-offs accepted for a single-user app. Please read it before you store anything important.

**Found a security issue?** Please open an issue describing it, without exploit details, rather than a public PR.

## More docs

- [`docs/BACKEND_ARCHITECTURE.md`](docs/BACKEND_ARCHITECTURE.md): database layout, auth, sync, attachments
- [`docs/INFRA_AND_COST.md`](docs/INFRA_AND_COST.md): AWS resources and what they cost
- [`docs/SECURITY_AUDIT.md`](docs/SECURITY_AUDIT.md): the security self-audit
- [`docs/MCP_SERVER_PLAN.md`](docs/MCP_SERVER_PLAN.md): the MCP server for Claude
- [`docs/LINK_SUMMARIES_PLAN.md`](docs/LINK_SUMMARIES_PLAN.md): link previews

## Deliberately not done (yet)

- **Real-time conflict merging (CRDTs).** Last-write-wins is enough for one person on a couple of devices.
- **Multiple users.** The database is already split per user, so it would mostly mean adding sign-up.
- **Server-side search.** Every note is already on the device, so search runs there for free.
- **Revoking a single login token.** Changing your password signs out *all* devices, which covers the "lost my phone" case.

## Contributing

Issues and PRs are welcome. Please run `pytest` (from `backend/`) before opening a PR.

## License

[MIT](LICENSE)

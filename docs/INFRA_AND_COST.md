# Infra & Cost

Answers the direct questions: Lambda vs Google Functions vs EC2, what exact resources are needed, and what this actually costs.

## Lambda vs Google Cloud Functions vs EC2

- **EC2 is correctly ruled out.** It's a box you pay for whether you use it or not (even the smallest `t4g.nano` is a few $/month, 24/7), sized for a workload that, for one person, is bursty and tiny. No reason to manage an OS for this.
- **Lambda vs Cloud Functions** — both are the right *shape* (pay-per-invocation, zero idle cost). Going with **AWS Lambda**:
  - AWS's free tier for Lambda (1M requests + 400,000 GB-seconds compute/month) is **permanent**, not a 12-month trial.
  - DynamoDB's free tier (25GB storage, 25 RCU/25 WCU) is **also permanent**. Pairing Lambda with DynamoDB keeps everything in one perpetually-free bracket.
  - GCP's free tier is comparable for Cloud Functions + Firestore, but since there's no existing GCP footprint to leverage, there's no reason to prefer it over AWS's slightly more generous/mature free tier here. (If you already had GCP credits or familiarity, this would be a closer call — flagged in case that's actually true and changes your pick.)
- **Skipping API Gateway**: a Lambda **Function URL** (a direct HTTPS endpoint on the function, with its own CORS config) does everything this app needs — a single REST-ish API, no API Gateway features (custom auth, request transformation, etc.) are actually used. This avoids API Gateway's request charges entirely (its free tier is 12-months-only, then ~$1/million requests — avoidable, not just cheap).

## Full resource list

| Resource | Purpose | Cost |
|---|---|---|
| **Lambda function** | Runs the FastAPI app (via Mangum) | Free tier: 1M req + 400k GB-s/mo, forever |
| **Lambda Function URL** | HTTPS entrypoint, replaces API Gateway | Free (no separate charge) |
| **DynamoDB table** (`yaadi`) | Notes, folders, attachment metadata | Free tier: 25GB + 25 RCU/WCU, forever |
| **DynamoDB GSI1** | Sync query ("changed since X") | Counted against the same free RCU/WCU pool |
| **S3 bucket** | Attachments (images/files) | Free tier: 5GB storage for 12 months, then ~$0.023/GB/mo — pennies at personal-note-app volume |
| **SSM Parameter Store** (`SecureString`) | Password hash, JWT secret | Free (Standard tier) |
| **IAM role** | Scopes the Lambda to only its own table + bucket | Free |
| **Vercel** (frontend hosting) | Serves the React PWA | Free tier (personal projects) |
| **Route53 + ACM** (optional) | Custom domain + HTTPS cert | ~$12/year for the domain if you want one; cert is free. Not required — Vercel and the Lambda Function URL both give you a working HTTPS URL with no domain purchase. |

**Realistic monthly cost: $0.** The only real cost is an optional custom domain, which is a yearly domain registration fee, not infrastructure.

## Deployment tooling

**AWS SAM** (`sam build`, `sam deploy`) — a thin, declarative `template.yaml` describing the Lambda, its Function URL, the DynamoDB table, the S3 bucket, and the IAM role. Chosen over raw Terraform/CDK because it's the least ceremony for "one Lambda + a couple of resources" — this isn't a team infra setup, it doesn't need a heavier IaC tool.

Frontend deploys via Vercel's git integration — push to `main`, it builds and deploys automatically. No separate CI/CD pipeline needed.

## Environments

Just one — **production**. For a single-user app there's no real need for a separate staging environment; test locally (`sam local start-api`, Vite dev server against a local or scratch DynamoDB table) before deploying.

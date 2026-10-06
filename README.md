# Yaadi — Personal Note-Taking App

A single-user, Obsidian/Notion-flavored note app, built to work flawlessly offline on your phone and laptop, with near-zero hosting cost.

This is a **planning repo** right now — no code yet. Read the docs below before writing any code; each covers one layer and links back here.

## Decisions locked in (from the planning conversation)

| Question | Decision | Why |
|---|---|---|
| Offline support | **Offline-first with sync** | You want it to "just work" like Obsidian, signal or not. |
| App delivery | **PWA** (installable web app) | One React codebase → phone + laptop, no app store fees, free hosting. |
| Note format | **Markdown documents** | Simple schema, trivially exportable/backup-able, matches Obsidian mental model. |
| Cloud | **AWS** (Lambda + DynamoDB) | Both have a *perpetual* free tier (not just 12 months) — realistically $0/mo for one user. |
| Auth | **Fixed single-user password login** | No signup flow, no multi-tenant complexity — just you. |

## Docs

- [`docs/BACKEND_ARCHITECTURE.md`](docs/BACKEND_ARCHITECTURE.md) — FastAPI on Lambda, DynamoDB schema, auth, sync API, attachments.
- [`docs/APP_ARCHITECTURE.md`](docs/APP_ARCHITECTURE.md) — PWA, local-first storage, the offline sync engine, editor choice.
- [`docs/DESIGN.md`](docs/DESIGN.md) — lightweight visual language (palette, layout, type) so UI stays consistent.
- [`docs/INFRA_AND_COST.md`](docs/INFRA_AND_COST.md) — exact AWS resources, why Lambda over EC2/GCP, cost table, deploy tooling.

## Tech stack at a glance

- **Frontend**: React + Vite, PWA (installable), CodeMirror 6 markdown editor, Dexie.js (IndexedDB) for local-first storage.
- **Backend**: FastAPI, wrapped for Lambda via Mangum.
- **Database**: DynamoDB (single-table design).
- **File storage**: S3 (for pasted images/attachments).
- **Auth**: single-user password login → JWT access + refresh tokens. No OAuth, no user table.
- **Infra**: AWS Lambda (Function URL, not API Gateway), DynamoDB, S3, SSM Parameter Store — deployed via AWS SAM.

## Open items / future enhancements (deliberately deferred, not forgotten)

- Full CRDT-based conflict resolution — using simple last-write-wins for now since it's one person on 1-2 devices; real concurrent-edit conflicts are rare. Revisit only if it ever causes a real lost edit.
- Multi-user support — the DynamoDB schema is already partitioned by `USER#<id>` so this wouldn't require a redesign, just an added signup flow.
- Server-side full-text search (e.g. OpenSearch) — not needed because offline-first means every note already lives on-device, so search runs client-side for free.
- Custom domain — optional, ~$12/year, not required to use the app.

## Questions still open for you

- **App name / domain**: using "Yaadi" (matches this folder) unless you want something else.
- **Device count**: roughly how many devices (phone + laptop? more?) will sync — doesn't change architecture, just worth confirming assumptions in the sync doc hold.
- **Backup/export**: do you want a "export all notes as .md files" button from day one, or is that a later nice-to-have?

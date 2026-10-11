# Link Previews — Plan

**Status:** backend built (`app/links.py`, `tests/test_links.py`), not yet deployed; frontend not started
**Date:** 2026-10-11
**Replaces:** the earlier Claude/`web_fetch` version of this plan (kept as the upgrade path at the bottom)

## Problem

Links shared into Yaadi land in the **Shared** folder as a bare URL. A few days later there's no way to tell what the link was about without opening it.

## The lazy answer: no AI, no crawler — read the page's own preview tags

Almost every article and repo page already carries a human-written summary for link previews (WhatsApp, Slack, Twitter cards): `og:title` and `og:description`. Reading those tags is all this feature needs. Python's standard library does it in ~40 lines, so there's **no new dependency, no API key and no cost**.

### Checked on real links (2026-10-11, stdlib `urllib` + `html.parser`)

| Link | Result |
|---|---|
| github.com/fastapi/fastapi | ✅ "FastAPI framework, high performance, easy to learn, fast to code, ready for production" |
| github.com/imputnet/cobalt | ✅ "best way to save what you love" |
| martinfowler.com article | ✅ "Defining the microservices architectural style by describing their nine common characteristics" |
| theverge.com, dev.to, x.com profile | ✅ title + description |
| youtube.com/watch | ❌ empty page → ✅ works through YouTube's free **oEmbed** endpoint (title + channel) |
| wikipedia, reddit, HN | ⚠️ title only, no description |
| medium.com, stackoverflow.com | ❌ 403 (bot protection) → note keeps just the URL, whose slug is already readable |

These were run from a home connection. **Re-check from Lambda after deploying**, since some sites block datacenter IPs harder.

### Why not a crawler / AI

| Option | Verdict |
|---|---|
| **`og:` tags via stdlib** | ✅ Covers articles and GitHub, $0, ~40 lines |
| trafilatura / readability (main-text extraction) | ❌ Gives the full article text, which is too long to put in a note. Turning it into a summary still needs an LLM. |
| Crawl4AI / Firecrawl / Playwright (headless browser) | ❌ Needs Chromium in Lambda: container image, 1–2 GB memory, slow cold starts. Only worth it for JS-only pages. |
| Extractive summarizers (sumy, etc.) | ❌ Mediocre output, and a new dependency |
| Claude + `web_fetch` | Later, only if descriptions turn out too thin (see "Upgrade path") |

---

## What the note looks like

```markdown
https://github.com/fastapi/fastapi

> **FastAPI**
> FastAPI framework, high performance, easy to learn, fast to code, ready for production
```

If the share came in without a title, the note's title becomes the page title instead of `Untitled`.

---

## Backend (`Yaadi/backend`)

### `app/links.py`: new router, `GET /links/preview?url=…`

- Protected by `get_current_user`, like every other router; mounted in `main.py`
- `url: HttpUrl` (pydantic, already installed) → anything that isn't http/https is a 422
- Always returns `200 {"title": str | null, "description": str | null}`. Any fetch or parse failure just gives nulls. Nothing to retry, nothing for the client to branch on.
- Fetch rules:
  - `urllib.request`, browser-like `User-Agent`, **5 s timeout**, read at most **512 KB** (the tags are in `<head>`)
  - Decode with the response charset, falling back to utf-8, `errors="replace"`
  - Parse with `html.parser.HTMLParser`: first `og:title` → `twitter:title` → `<title>`; `og:description` → `description` → `twitter:description`
  - YouTube (`youtube.com` / `youtu.be`): call `https://www.youtube.com/oembed?url=…&format=json` instead, returning `title` and `author_name`
- **SSRF guard (required):** before fetching, and again on every redirect (a small `HTTPRedirectHandler` subclass), resolve the host and refuse unless every address is `ipaddress.ip_address(...).is_global`. Without it, the endpoint could reach the Lambda Runtime API on `127.0.0.1:9001`. DNS rebinding between the check and the fetch remains possible; that's acceptable because the endpoint needs your own login token.
- Existing 10 s Lambda timeout is enough. **No `template.yaml` change, no SSM change, no new requirements.**

### `tests/test_links.py`

Monkeypatch `urllib.request` with canned HTML. No real network in tests, the same approach as moto.
- og tags parsed; falls back to `<title>` / `name="description"`
- fetch raises → 200 with nulls
- `ftp://…` → 422; `http://127.0.0.1:9001/` and a redirect to `169.254.169.254` → nulls (blocked)
- no auth → 401

---

## Frontend (`yaadi-app`)

### `src/routes/ShareTarget.tsx`: the only file that changes

Current order: create note → update → navigate. New order:

1. Create the note and save the shared text + URL, **exactly as today**. The share is safe in IndexedDB before any network call.
2. `link = url || text.match(/https?:\/\/\S+/)?.[0]` (Instagram and many Android apps put the link in `text`)
3. If there's a link: `apiFetch('/links/preview?url=…', { signal: AbortSignal.timeout(6000) })`. Offline, timeout or error all mean no preview, and that's fine.
4. If a title or description came back: `updateNote()` once more with the `> **title**` block appended and, if needed, the title
5. Navigate to the editor, as now

**Why fetch before opening the editor:** `Editor.tsx` loads a note's content into CodeMirror only when the note ID changes (`Editor.tsx:96-154`). Writing the preview into a note that's already open would be invisible, and the next keystroke would save the editor's stale copy over it. Doing it on the share screen ("Keeping this…") avoids that race with no extra code.

Cost to you: the share screen waits up to ~6 s on a slow link, instead of the current 0.9 s.

### Skipped on purpose

- **No offline queue.** A link shared while offline just gets no preview. Mark it in code: `// shortcut: offline shares get no preview, add a Dexie retry queue if that turns out to matter`.
- No "Summarizing…" UI, no re-fetch button, no previews for links pasted by hand.

---

## Security notes (add to `SECURITY_AUDIT.md` when shipped)

- New outbound fetch of user-supplied URLs, guarded against private, loopback and link-local targets, including on redirects. DNS rebinding is an accepted residual risk for a single-principal app behind auth.
- No new secrets. No data leaves AWS except the request to the page itself.

## Build order

1. `links.py` + tests → `sam build && sam deploy`
2. Re-run the link check through the deployed endpoint, to see what Lambda IPs get blocked
3. `ShareTarget.tsx` change
4. Docs: `BACKEND_ARCHITECTURE.md` (endpoint), `SECURITY_AUDIT.md`, `CLAUDE.md` (mention `links.py`)

---

## Upgrade path (only if v1 isn't enough)

If preview descriptions turn out too thin to jog your memory, keep the same endpoint and response shape, and change what's behind it:
- **Real summaries:** send the URL to Claude with the `web_fetch` server tool (`claude-opus-5-5`, ~2–3¢/link; `claude-haiku-5-5`, well under 1¢). It needs an Anthropic API key in SSM and a 30 s Lambda timeout. The client doesn't change.
- **Sites that return 403 (Medium, StackOverflow):** the same Claude route, since Anthropic fetches from its own servers.
- **Spoken-word videos:** self-hosted cobalt + speech-to-text. That's a separate server, so only worth it if you save many reels.

"""FastAPI() instance, Mangum handler.

CORS is handled entirely by the Lambda Function URL's own CORS config
(template.yaml) -- not here. The Function URL intercepts OPTIONS preflight
requests at the platform level without even invoking this function, and
adding CORSMiddleware here too would emit a second, conflicting
Access-Control-Allow-Origin header on every response.
"""
import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

import attachments
import auth
import folders
import links
import mcp_server
import notes
import security
import sync

# Lambda always sets this, so "running in Lambda" == prod: no public
# /docs, /redoc or /openapi.json there. Local runs and tests keep them.
IN_LAMBDA = "AWS_LAMBDA_FUNCTION_NAME" in os.environ

app = FastAPI(
    title="Yaadi API",
    docs_url=None if IN_LAMBDA else "/docs",
    redoc_url=None if IN_LAMBDA else "/redoc",
    openapi_url=None if IN_LAMBDA else "/openapi.json",
)

@app.middleware("http")
async def require_cloudflare_worker(request: Request, call_next):
    """In Lambda, only answer requests forwarded by the Cloudflare Worker
    (yaadi-app/worker.js), which adds X-Origin-Secret. That makes the raw
    Function URL useless on its own, and lets auth.py trust the Worker-set
    X-Client-IP for the per-IP login lockout."""
    if IN_LAMBDA:
        sent = request.headers.get("x-origin-secret", "").encode()
        if not hmac.compare_digest(sent, security.get_origin_secret().encode()):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)


app.include_router(auth.router)
app.include_router(notes.router)
app.include_router(folders.router)
app.include_router(sync.router)
app.include_router(attachments.router)
app.include_router(links.router)
app.include_router(mcp_server.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


handler = Mangum(app)

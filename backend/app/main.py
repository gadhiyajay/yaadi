"""FastAPI() instance, Mangum handler.

CORS is handled entirely by the Lambda Function URL's own CORS config
(template.yaml) -- not here. The Function URL intercepts OPTIONS preflight
requests at the platform level without even invoking this function, and
adding CORSMiddleware here too would emit a second, conflicting
Access-Control-Allow-Origin header on every response.
"""
from fastapi import FastAPI
from mangum import Mangum

import attachments
import auth
import folders
import notes
import sync

app = FastAPI(title="Yaadi API")

app.include_router(auth.router)
app.include_router(notes.router)
app.include_router(folders.router)
app.include_router(sync.router)
app.include_router(attachments.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


handler = Mangum(app)

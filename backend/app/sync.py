"""GET /sync/pull, POST /sync/push -- see docs/APP_ARCHITECTURE.md for the client half.

Last-write-wins by `updated_at`, no operational transforms.
"""
from fastapi import APIRouter, Depends

import db
from auth import get_current_user
from folders import folder_to_item, item_to_folder
from models import (
    Folder,
    Note,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
    SyncPushResult,
)
from notes import item_to_note, note_to_item

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(get_current_user)])

EPOCH = "1970-01-01T00:00:00+00:00"


@router.get("/pull", response_model=SyncPullResponse)
def pull(since: str = EPOCH) -> SyncPullResponse:
    items = db.query_updated_since(since)
    notes = [item_to_note(i) for i in items if i["SK"].startswith("NOTE#")]
    folders = [item_to_folder(i) for i in items if i["SK"].startswith("FOLDER#")]

    # The next cursor must never run ahead of data we've actually seen.
    # `updated_at` is set by each client's own clock (see _push_note/_push_
    # folder below), which can drift from this server's clock -- stamping
    # the cursor with the server's `now()` risked permanently skipping any
    # item whose client-assigned `updated_at` fell behind that `now()`
    # before it had a chance to sync. Anchoring to the max `updated_at` we
    # actually returned (or leaving the cursor unmoved when nothing came
    # back) only ever moves the cursor as far as confirmed data.
    synced_at = max((i["updated_at"] for i in items), default=since)
    return SyncPullResponse(
        notes=notes,
        folders=folders,
        synced_at=synced_at,
    )


@router.post("/push", response_model=SyncPushResponse)
def push(body: SyncPushRequest) -> SyncPushResponse:
    results = [_push_item(entry.item_type, entry.data) for entry in body.items]
    return SyncPushResponse(results=results)


def _push_item(item_type: str, data: dict) -> SyncPushResult:
    if item_type == "note":
        return _push_note(data)
    if item_type == "folder":
        return _push_folder(data)
    return SyncPushResult(item_type=item_type, id=data.get("id", ""), status="rejected")


def _push_note(data: dict) -> SyncPushResult:
    incoming = Note(**data)
    existing = db.get_item(db.note_sk(incoming.id))
    if existing and existing["updated_at"] >= incoming.updated_at:
        return SyncPushResult(
            item_type="note",
            id=incoming.id,
            status="rejected",
            server_item=item_to_note(existing).model_dump(),
        )
    db.put_item(note_to_item(incoming))
    return SyncPushResult(item_type="note", id=incoming.id, status="accepted")


def _push_folder(data: dict) -> SyncPushResult:
    incoming = Folder(**data)
    existing = db.get_item(db.folder_sk(incoming.id))
    if existing and existing["updated_at"] >= incoming.updated_at:
        return SyncPushResult(
            item_type="folder",
            id=incoming.id,
            status="rejected",
            server_item=item_to_folder(existing).model_dump(),
        )
    db.put_item(folder_to_item(incoming))
    return SyncPushResult(item_type="folder", id=incoming.id, status="accepted")

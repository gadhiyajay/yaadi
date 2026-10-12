"""GET /sync/pull, POST /sync/push -- see docs/APP_ARCHITECTURE.md for the client half.

Last-write-wins by `updated_at`, no operational transforms.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import ValidationError

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

# How long a write can take to show up in a pull: GSI1 is eventually
# consistent, and two Lambdas can stamp writes in one order and land them in
# the other.
SETTLE_SECONDS = 5

_PUSH_TYPES = {
    "note": (Note, db.note_sk, note_to_item, item_to_note),
    "folder": (Folder, db.folder_sk, folder_to_item, item_to_folder),
}


@router.get("/pull", response_model=SyncPullResponse)
def pull(since: str = EPOCH) -> SyncPullResponse:
    items = db.query_updated_since(since)
    notes = [item_to_note(i) for i in items if i["SK"].startswith("NOTE#")]
    folders = [item_to_folder(i) for i in items if i["SK"].startswith("FOLDER#")]

    # The cursor walks GSI1SK, which holds the *server's* write time (see
    # note_to_item/folder_to_item), never a client's `updated_at`. A cursor
    # built from client clocks loses data: a device running behind pushes
    # an item stamped earlier than a cursor another device already holds,
    # and that device never pulls it.
    #
    # It also stops SETTLE_SECONDS short of now: a write stamped just before
    # this query may not be visible in GSI1 yet, and the cursor must not
    # pass it. Items in that window come back again on the next pull, which
    # the client already handles (it upserts by id).
    seen = max((i["GSI1SK"].split("#")[1] for i in items), default=since)
    settled = (datetime.now(timezone.utc) - timedelta(seconds=SETTLE_SECONDS)).isoformat(
        timespec="microseconds"
    )
    return SyncPullResponse(notes=notes, folders=folders, synced_at=max(since, min(seen, settled)))


@router.post("/push", response_model=SyncPushResponse)
def push(body: SyncPushRequest) -> SyncPushResponse:
    return SyncPushResponse(results=[_push_item(entry.item_type, entry.data) for entry in body.items])


def _push_item(item_type: str, data: dict) -> SyncPushResult:
    item_id = str(data.get("id", ""))
    if item_type not in _PUSH_TYPES:
        return SyncPushResult(item_type=item_type, id=item_id, status="rejected")
    model, sk, to_item, from_item = _PUSH_TYPES[item_type]

    # Checked one item at a time: raising here would 500 the whole batch,
    # and the client never clears its queue on a failed push, so one bad
    # item would block that device's sync for good.
    try:
        incoming = model(**data)
    except ValidationError:
        return SyncPushResult(item_type=item_type, id=item_id, status="rejected")

    # A device with its clock running ahead would otherwise stamp a future
    # `updated_at`, and last-write-wins would reject every real edit until
    # that time.
    now = db.now()
    if incoming.updated_at > now:
        incoming = incoming.model_copy(update={"updated_at": now})

    if db.put_if_newer(to_item(incoming)):
        return SyncPushResult(item_type=item_type, id=incoming.id, status="accepted")
    existing = db.get_item(sk(incoming.id))
    return SyncPushResult(
        item_type=item_type,
        id=incoming.id,
        status="rejected",
        server_item=from_item(existing).model_dump(),
    )

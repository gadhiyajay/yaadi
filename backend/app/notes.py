"""/notes CRUD."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import db
from auth import get_current_user
from models import Note, NoteCreate, NoteUpdate

router = APIRouter(prefix="/notes", tags=["notes"], dependencies=[Depends(get_current_user)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def item_to_note(item: dict) -> Note:
    return Note(
        id=item["SK"].removeprefix("NOTE#"),
        title=item.get("title", ""),
        content=item.get("content", ""),
        folder_id=item.get("folder_id"),
        tags=list(item.get("tags", [])),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        deleted_at=item.get("deleted_at"),
    )


def note_to_item(note: Note) -> dict:
    item = {
        "PK": db.user_pk(),
        "SK": db.note_sk(note.id),
        "GSI1PK": db.user_pk(),
        "GSI1SK": db.gsi1_sk(note.updated_at, note.id),
        "title": note.title,
        "content": note.content,
        "folder_id": note.folder_id,
        "tags": note.tags,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }
    if note.deleted_at:
        item["deleted_at"] = note.deleted_at
    return item


@router.get("", response_model=list[Note])
def list_notes() -> list[Note]:
    items = db.query_by_prefix("NOTE#")
    return [item_to_note(i) for i in items if not i.get("deleted_at")]


@router.get("/{note_id}", response_model=Note)
def get_note(note_id: str) -> Note:
    item = db.get_item(db.note_sk(note_id))
    if item is None or item.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Note not found")
    return item_to_note(item)


@router.post("", response_model=Note, status_code=201)
def create_note(body: NoteCreate) -> Note:
    now = _now()
    note = Note(
        id=str(uuid.uuid4()),
        title=body.title,
        content=body.content,
        folder_id=body.folder_id,
        tags=body.tags,
        created_at=now,
        updated_at=now,
    )
    db.put_item(note_to_item(note))
    return note


@router.patch("/{note_id}", response_model=Note)
def update_note(note_id: str, body: NoteUpdate) -> Note:
    item = db.get_item(db.note_sk(note_id))
    if item is None or item.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Note not found")
    note = item_to_note(item)
    changes = body.model_dump(exclude_unset=True)
    updated = note.model_copy(update={**changes, "updated_at": _now()})
    db.put_item(note_to_item(updated))
    return updated


@router.delete("/{note_id}", status_code=204)
def delete_note(note_id: str) -> None:
    item = db.get_item(db.note_sk(note_id))
    if item is None or item.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Note not found")
    note = item_to_note(item)
    now = _now()
    deleted = note.model_copy(update={"deleted_at": now, "updated_at": now})
    db.put_item(note_to_item(deleted))

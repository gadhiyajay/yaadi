"""/folders CRUD."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import db
from auth import get_current_user
from models import Folder, FolderCreate, FolderUpdate

router = APIRouter(prefix="/folders", tags=["folders"], dependencies=[Depends(get_current_user)])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def item_to_folder(item: dict) -> Folder:
    return Folder(
        id=item["SK"].removeprefix("FOLDER#"),
        name=item.get("name", ""),
        parent_folder_id=item.get("parent_folder_id"),
        created_at=item["created_at"],
        updated_at=item["updated_at"],
        deleted_at=item.get("deleted_at"),
    )


def folder_to_item(folder: Folder) -> dict:
    item = {
        "PK": db.user_pk(),
        "SK": db.folder_sk(folder.id),
        "GSI1PK": db.user_pk(),
        "GSI1SK": db.gsi1_sk(folder.updated_at, folder.id),
        "name": folder.name,
        "parent_folder_id": folder.parent_folder_id,
        "created_at": folder.created_at,
        "updated_at": folder.updated_at,
    }
    if folder.deleted_at:
        item["deleted_at"] = folder.deleted_at
    return item


@router.get("", response_model=list[Folder])
def list_folders() -> list[Folder]:
    items = db.query_by_prefix("FOLDER#")
    return [item_to_folder(i) for i in items if not i.get("deleted_at")]


@router.get("/{folder_id}", response_model=Folder)
def get_folder(folder_id: str) -> Folder:
    item = db.get_item(db.folder_sk(folder_id))
    if item is None or item.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Folder not found")
    return item_to_folder(item)


@router.post("", response_model=Folder, status_code=201)
def create_folder(body: FolderCreate) -> Folder:
    now = _now()
    folder = Folder(
        id=str(uuid.uuid4()),
        name=body.name,
        parent_folder_id=body.parent_folder_id,
        created_at=now,
        updated_at=now,
    )
    db.put_item(folder_to_item(folder))
    return folder


@router.patch("/{folder_id}", response_model=Folder)
def update_folder(folder_id: str, body: FolderUpdate) -> Folder:
    item = db.get_item(db.folder_sk(folder_id))
    if item is None or item.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Folder not found")
    folder = item_to_folder(item)
    changes = body.model_dump(exclude_unset=True)
    updated = folder.model_copy(update={**changes, "updated_at": _now()})
    db.put_item(folder_to_item(updated))
    return updated


@router.delete("/{folder_id}", status_code=204)
def delete_folder(folder_id: str) -> None:
    item = db.get_item(db.folder_sk(folder_id))
    if item is None or item.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Folder not found")
    folder = item_to_folder(item)
    now = _now()
    deleted = folder.model_copy(update={"deleted_at": now, "updated_at": now})
    db.put_item(folder_to_item(deleted))

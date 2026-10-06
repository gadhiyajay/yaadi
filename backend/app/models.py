"""Pydantic request/response models."""
from typing import Optional

from pydantic import BaseModel, Field

# --- auth ---


class LoginRequest(BaseModel):
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# --- notes ---


class Note(BaseModel):
    id: str
    title: str = ""
    content: str = ""
    folder_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    deleted_at: Optional[str] = None


class NoteCreate(BaseModel):
    title: str = ""
    content: str = ""
    folder_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    folder_id: Optional[str] = None
    tags: Optional[list[str]] = None


# --- folders ---


class Folder(BaseModel):
    id: str
    name: str
    parent_folder_id: Optional[str] = None
    created_at: str
    updated_at: str
    deleted_at: Optional[str] = None


class FolderCreate(BaseModel):
    name: str
    parent_folder_id: Optional[str] = None


class FolderUpdate(BaseModel):
    name: Optional[str] = None
    parent_folder_id: Optional[str] = None


# --- attachments ---


class Attachment(BaseModel):
    id: str
    note_id: str
    s3_key: str
    content_type: str
    size_bytes: int
    created_at: str


class PresignUploadRequest(BaseModel):
    note_id: str
    content_type: str


class PresignUploadResponse(BaseModel):
    upload_url: str
    attachment_id: str
    s3_key: str


class DownloadUrlResponse(BaseModel):
    download_url: str


# --- sync ---


class SyncPushItem(BaseModel):
    item_type: str  # "note" | "folder"
    data: dict


class SyncPushRequest(BaseModel):
    items: list[SyncPushItem]


class SyncPushResult(BaseModel):
    item_type: str
    id: str
    status: str  # "accepted" | "rejected"
    server_item: Optional[dict] = None


class SyncPushResponse(BaseModel):
    results: list[SyncPushResult]


class SyncPullResponse(BaseModel):
    notes: list[Note]
    folders: list[Folder]
    synced_at: str

"""POST /attachments/presign-upload, GET /attachments/{id}/download-url.

Files go direct to/from S3 via pre-signed URLs -- never through the Lambda
payload. See docs/BACKEND_ARCHITECTURE.md.
"""
import os
import uuid

import boto3
from fastapi import APIRouter, Depends, HTTPException

import db
from auth import get_current_user
from models import DownloadUrlResponse, PresignUploadRequest, PresignUploadResponse

router = APIRouter(
    prefix="/attachments", tags=["attachments"], dependencies=[Depends(get_current_user)]
)

BUCKET_NAME = os.environ.get("ATTACHMENTS_BUCKET", "")
PRESIGN_EXPIRY_SECONDS = 300

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


@router.post("/presign-upload", response_model=PresignUploadResponse)
def presign_upload(body: PresignUploadRequest) -> PresignUploadResponse:
    attachment_id = str(uuid.uuid4())
    s3_key = f"{db.USER_ID}/{attachment_id}"
    upload_url = _s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET_NAME, "Key": s3_key, "ContentType": body.content_type},
        ExpiresIn=PRESIGN_EXPIRY_SECONDS,
    )

    now = db.now()
    item = {
        "PK": db.user_pk(),
        "SK": db.attachment_sk(attachment_id),
        "GSI1PK": db.user_pk(),
        "GSI1SK": db.gsi1_sk(now, attachment_id),
        "note_id": body.note_id,
        "s3_key": s3_key,
        "content_type": body.content_type,
        "size_bytes": 0,
        "created_at": now,
    }
    db.put_item(item)

    return PresignUploadResponse(upload_url=upload_url, attachment_id=attachment_id, s3_key=s3_key)


@router.get("/{attachment_id}/download-url", response_model=DownloadUrlResponse)
def get_download_url(attachment_id: str) -> DownloadUrlResponse:
    item = db.get_item(db.attachment_sk(attachment_id))
    if item is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    url = _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": item["s3_key"]},
        ExpiresIn=PRESIGN_EXPIRY_SECONDS,
    )
    return DownloadUrlResponse(download_url=url)

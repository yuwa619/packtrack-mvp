from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..config import settings
from ..db.models import Document, Job, UploadSession
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context
from ..services.audit import add_audit_event
from ..services.idempotency import IdempotencyGuard
from ..services.queue import JobQueue
from ..services.storage import ObjectStorage

router = APIRouter(prefix="/documents", tags=["documents"])

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
}


class PresignUploadRequest(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int


class FinaliseUploadRequest(BaseModel):
    upload_id: UUID


def _validate_upload_metadata(*, mime_type: str, size_bytes: int) -> None:
    if mime_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported mime type: {mime_type}")
    if size_bytes <= 0:
        raise HTTPException(status_code=400, detail="File size must be positive")
    if size_bytes > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds max size ({settings.max_upload_size_bytes} bytes)",
        )


@router.post("/upload/presign")
def create_upload_url(
    request: PresignUploadRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict[str, str | int]:
    _validate_upload_metadata(mime_type=request.mime_type, size_bytes=request.size_bytes)

    upload_id = uuid4()
    filename = Path(request.filename).name
    bucket_name = settings.minio_bucket_raw
    object_key = f"raw-uploads/{auth.tenant_id}/{upload_id}/{filename}"

    storage = ObjectStorage()
    upload_url = storage.create_presigned_put_url(
        bucket=bucket_name,
        key=object_key,
        expires_seconds=settings.upload_url_expiry_seconds,
    )

    with db_session() as session:
        upload_session = UploadSession(
            id=upload_id,
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            filename=filename,
            mime_type=request.mime_type,
            expected_size_bytes=request.size_bytes,
            bucket_name=bucket_name,
            object_key=object_key,
            status="PRESIGNED",
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=settings.upload_url_expiry_seconds),
        )
        session.add(upload_session)

        add_audit_event(
            session=session,
            event_type="UPLOAD_URL_CREATED",
            entity_type="upload_session",
            entity_id=str(upload_id),
            payload={
                "tenant_id": auth.tenant_id,
                "user_id": auth.user_id,
                "object_key": object_key,
                "size_bytes": request.size_bytes,
            },
        )

    return {
        "upload_id": str(upload_id),
        "upload_url": upload_url,
        "bucket": bucket_name,
        "object_key": object_key,
        "expires_in": settings.upload_url_expiry_seconds,
    }


@router.post("/upload/finalise")
def finalise_upload(
    request: FinaliseUploadRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, str]:
    storage = ObjectStorage()
    queue = JobQueue()

    with db_session() as session:
        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="upload_finalise",
            idempotency_key=idempotency_key,
            request_payload={"upload_id": str(request.upload_id)},
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        try:
            upload_session = (
                session.execute(
                    select(UploadSession).where(
                        UploadSession.id == request.upload_id,
                        UploadSession.tenant_id == auth.tenant_id,
                    )
                )
                .scalars()
                .first()
            )
            if upload_session is None:
                raise HTTPException(status_code=404, detail="Upload session not found")

            if upload_session.status == "FINALISED":
                raise HTTPException(status_code=409, detail="Upload session already finalised")
            expires_at = upload_session.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                raise HTTPException(status_code=410, detail="Upload session has expired")

            _validate_upload_metadata(
                mime_type=upload_session.mime_type,
                size_bytes=upload_session.expected_size_bytes,
            )

            try:
                file_bytes = storage.get_bytes(
                    bucket=upload_session.bucket_name,
                    key=upload_session.object_key,
                )
            except FileNotFoundError as exc:
                raise HTTPException(status_code=409, detail="Uploaded object not found") from exc

            actual_size = len(file_bytes)
            if actual_size != upload_session.expected_size_bytes:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Uploaded size does not match expected size "
                        f"({actual_size} != {upload_session.expected_size_bytes})"
                    ),
                )
            if actual_size > settings.max_upload_size_bytes:
                raise HTTPException(status_code=400, detail="Uploaded object exceeds max size")

            checksum = hashlib.sha256(file_bytes).hexdigest()
            document_id = uuid4()
            document_uri = storage.build_uri(upload_session.bucket_name, upload_session.object_key)

            document = Document(
                id=document_id,
                organisation_id=auth.tenant_id,
                subsidiary_id="",
                organisation_size="L",
                submission_period="2025-P1",
                original_filename=upload_session.filename,
                mime_type=upload_session.mime_type,
                file_size_bytes=actual_size,
                checksum_sha256=checksum,
                uploaded_by=auth.user_id,
                storage_path=document_uri,
                status="QUEUED",
            )
            session.add(document)

            job_id = uuid4()
            job = Job(
                id=job_id,
                document_id=document_id,
                organisation_id=auth.tenant_id,
                status="QUEUED",
                current_stage="QUEUED",
                queue_name=settings.processing_queue_name,
                attempt_count=0,
                error_message=None,
            )
            session.add(job)

            queue_payload = {
                "job_id": str(job_id),
                "document_id": str(document_id),
                "tenant_id": auth.tenant_id,
                "stage": "PREPROCESSING",
            }
            queue.enqueue(settings.processing_queue_name, queue_payload)

            upload_session.status = "FINALISED"
            upload_session.finalised_at = datetime.now(timezone.utc)
            session.add(upload_session)

            add_audit_event(
                session=session,
                event_type="UPLOAD_FINALISED",
                entity_type="upload_session",
                entity_id=str(upload_session.id),
                payload={"document_id": str(document_id), "job_id": str(job_id)},
            )
            add_audit_event(
                session=session,
                event_type="JOB_CREATED",
                entity_type="job",
                entity_id=str(job_id),
                payload={
                    "document_id": str(document_id),
                    "tenant_id": auth.tenant_id,
                    "queue": settings.processing_queue_name,
                },
            )
            add_audit_event(
                session=session,
                event_type="JOB_ENQUEUED",
                entity_type="job",
                entity_id=str(job_id),
                payload=queue_payload,
            )

            response = {
                "upload_id": str(request.upload_id),
                "document_id": str(document_id),
                "job_id": str(job_id),
                "status": "QUEUED",
            }
            idempotency.success(response)
            return response
        except HTTPException as exc:
            idempotency.failure(status_code=exc.status_code, detail=str(exc.detail))
            raise
        except Exception as exc:
            idempotency.failure(status_code=500, detail=str(exc))
            raise

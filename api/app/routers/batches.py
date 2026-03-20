from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..config import settings
from ..db.models import Batch, BatchDocument, Document, Job, Report, UploadSession
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context
from ..services.audit import add_audit_event
from ..services.idempotency import IdempotencyGuard
from ..services.pipeline_runner import PipelineRunner
from ..services.queue import JobQueue
from ..services.report_export import render_report_csv
from ..services.storage import ObjectStorage
from .documents import _validate_upload_metadata

router = APIRouter(prefix="/batches", tags=["batches"])

ZIP_MIME_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
}
ZIP_SUPPORTED_SUFFIXES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
}
ZIP_EXECUTABLE_SUFFIXES = {
    ".app",
    ".bat",
    ".bin",
    ".cmd",
    ".com",
    ".command",
    ".dll",
    ".exe",
    ".jar",
    ".msi",
    ".py",
    ".sh",
}


class BatchFileRequest(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int


class BatchCreateRequest(BaseModel):
    name: str | None = None
    files: list[BatchFileRequest]


class BatchFinaliseRequest(BaseModel):
    upload_ids: list[UUID]


class ZipBatchPresignRequest(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int
    name: str | None = None


class ZipBatchFinaliseRequest(BaseModel):
    upload_id: UUID


def _get_batch(*, session, batch_id: UUID, tenant_id: int) -> Batch:
    batch = (
        session.execute(select(Batch).where(Batch.id == batch_id, Batch.tenant_id == tenant_id))
        .scalars()
        .first()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")
    return batch


def _latest_job_for_document(*, session, document_id: UUID) -> Job | None:
    return (
        session.execute(
            select(Job)
            .where(Job.document_id == document_id)
            .order_by(Job.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def _ensure_batch_document_link(*, session, batch_id: UUID, document_id: UUID) -> None:
    existing = (
        session.execute(
            select(BatchDocument).where(
                BatchDocument.batch_id == batch_id,
                BatchDocument.document_id == document_id,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return
    session.add(BatchDocument(batch_id=batch_id, document_id=document_id))


def _validate_zip_upload_metadata(*, mime_type: str, size_bytes: int) -> None:
    if mime_type not in ZIP_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported ZIP mime type: {mime_type}")
    if size_bytes <= 0:
        raise HTTPException(status_code=400, detail="ZIP file size must be positive")
    if size_bytes > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"ZIP file exceeds max size ({settings.max_upload_size_bytes} bytes)",
        )


def _create_batch_document(
    *,
    session,
    auth: AuthContext,
    batch: Batch,
    filename: str,
    mime_type: str,
    file_bytes: bytes,
    storage_uri: str,
    source: str,
    document_id: UUID | None = None,
) -> tuple[Document, Job]:
    actual_size = len(file_bytes)
    _validate_upload_metadata(mime_type=mime_type, size_bytes=actual_size)

    document = Document(
        id=document_id or uuid4(),
        organisation_id=auth.tenant_id,
        subsidiary_id="",
        organisation_size="L",
        submission_period="2025-P1",
        original_filename=filename,
        mime_type=mime_type,
        file_size_bytes=actual_size,
        checksum_sha256=hashlib.sha256(file_bytes).hexdigest(),
        uploaded_by=auth.user_id,
        storage_path=storage_uri,
        status="QUEUED",
    )
    session.add(document)
    session.flush()

    job = Job(
        id=uuid4(),
        document_id=document.id,
        organisation_id=auth.tenant_id,
        status="QUEUED",
        current_stage="QUEUED",
        queue_name=settings.processing_queue_name,
        attempt_count=0,
        error_message=None,
    )
    session.add(job)

    _ensure_batch_document_link(
        session=session,
        batch_id=batch.id,
        document_id=document.id,
    )
    add_audit_event(
        session=session,
        event_type="JOB_CREATED",
        entity_type="job",
        entity_id=str(job.id),
        payload={
            "document_id": str(document.id),
            "tenant_id": auth.tenant_id,
            "queue": settings.processing_queue_name,
            "batch_id": str(batch.id),
            "source": source,
        },
    )
    add_audit_event(
        session=session,
        event_type="BATCH_DOCUMENT_ADDED",
        entity_type="batch",
        entity_id=str(batch.id),
        payload={
            "document_id": str(document.id),
            "job_id": str(job.id),
            "filename": document.original_filename,
            "source": source,
        },
    )
    session.flush()
    return document, job


def _reject_zip_entry(
    *,
    rejected_files: list[dict[str, str]],
    filename: str,
    reason: str,
) -> None:
    rejected_files.append({"filename": filename, "reason": reason})


def _validate_zip_entry(
    *,
    archive_entry: zipfile.ZipInfo,
    total_uncompressed_bytes: int,
    accepted_count: int,
) -> tuple[str | None, str | None]:
    raw_name = archive_entry.filename.replace("\\", "/")
    path = PurePosixPath(raw_name)
    display_name = raw_name
    if archive_entry.is_dir() or raw_name.endswith("/"):
        return display_name, "Directories are not supported"
    if raw_name.startswith("/") or any(part in {"..", ""} for part in path.parts):
        return display_name, "Path traversal entry is not allowed"
    if any(part.startswith(".") for part in path.parts):
        return display_name, "Hidden files are not supported"

    filename = path.name
    suffix = Path(filename).suffix.lower()
    if suffix == ".zip":
        return filename, "Nested ZIP files are not supported"
    if suffix in ZIP_EXECUTABLE_SUFFIXES:
        return filename, "Executable files are not supported"
    if suffix not in ZIP_SUPPORTED_SUFFIXES:
        return filename, "Unsupported file type"
    if archive_entry.file_size > settings.max_upload_size_bytes:
        return filename, "File exceeds max size"
    if accepted_count >= settings.zip_max_file_count:
        return filename, "ZIP exceeds max file count"
    if (
        total_uncompressed_bytes + archive_entry.file_size
        > settings.zip_max_total_uncompressed_bytes
    ):
        return filename, "ZIP exceeds max total uncompressed size"
    return filename, None


def _finalise_upload_to_document(
    *,
    session,
    storage: ObjectStorage,
    upload_session: UploadSession,
    auth: AuthContext,
    batch: Batch,
) -> tuple[Document, Job]:
    if upload_session.batch_id != batch.id:
        raise HTTPException(status_code=409, detail="Upload session does not belong to this batch")

    document_uri = storage.build_uri(upload_session.bucket_name, upload_session.object_key)
    existing_document = (
        session.execute(
            select(Document).where(
                Document.organisation_id == auth.tenant_id,
                Document.storage_path == document_uri,
            )
        )
        .scalars()
        .first()
    )
    if upload_session.status == "FINALISED" and existing_document is not None:
        _ensure_batch_document_link(
            session=session,
            batch_id=batch.id,
            document_id=existing_document.id,
        )
        existing_job = _latest_job_for_document(session=session, document_id=existing_document.id)
        if existing_job is None:
            existing_job = Job(
                id=uuid4(),
                document_id=existing_document.id,
                organisation_id=auth.tenant_id,
                status="QUEUED",
                current_stage="QUEUED",
                queue_name=settings.processing_queue_name,
                attempt_count=0,
                error_message=None,
            )
            session.add(existing_job)
            session.flush()
        return existing_document, existing_job

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

    document, job = _create_batch_document(
        session=session,
        auth=auth,
        batch=batch,
        filename=upload_session.filename,
        mime_type=upload_session.mime_type,
        file_bytes=file_bytes,
        storage_uri=document_uri,
        source="direct_batch_upload",
    )

    upload_session.status = "FINALISED"
    upload_session.finalised_at = datetime.now(timezone.utc)
    session.add(upload_session)

    add_audit_event(
        session=session,
        event_type="UPLOAD_FINALISED",
        entity_type="upload_session",
        entity_id=str(upload_session.id),
        payload={
            "document_id": str(document.id),
            "job_id": str(job.id),
            "batch_id": str(batch.id),
        },
    )
    session.flush()
    return document, job


@router.post("")
def create_batch(
    request: BatchCreateRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    if not request.files:
        raise HTTPException(status_code=400, detail="At least one file is required")

    storage = ObjectStorage()
    with db_session() as session:
        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="batch_create",
            idempotency_key=idempotency_key,
            request_payload={
                "name": request.name,
                "files": [item.model_dump() for item in request.files],
            },
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        batch = Batch(
            id=uuid4(),
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            name=(request.name or "").strip() or None,
            status="PRESIGNED",
        )
        session.add(batch)
        session.flush()

        uploads: list[dict[str, object]] = []
        for file_request in request.files:
            _validate_upload_metadata(
                mime_type=file_request.mime_type,
                size_bytes=file_request.size_bytes,
            )
            upload_id = uuid4()
            filename = Path(file_request.filename).name
            object_key = f"raw-uploads/{auth.tenant_id}/batches/{batch.id}/{upload_id}/{filename}"
            upload_url = storage.create_presigned_put_url(
                bucket=settings.minio_bucket_raw,
                key=object_key,
                expires_seconds=settings.upload_url_expiry_seconds,
            )
            session.add(
                UploadSession(
                    id=upload_id,
                    batch_id=batch.id,
                    tenant_id=auth.tenant_id,
                    user_id=auth.user_id,
                    filename=filename,
                    mime_type=file_request.mime_type,
                    expected_size_bytes=file_request.size_bytes,
                    bucket_name=settings.minio_bucket_raw,
                    object_key=object_key,
                    status="PRESIGNED",
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=settings.upload_url_expiry_seconds),
                )
            )
            uploads.append(
                {
                    "upload_id": str(upload_id),
                    "filename": filename,
                    "upload_url": upload_url,
                    "bucket": settings.minio_bucket_raw,
                    "object_key": object_key,
                    "expires_in": settings.upload_url_expiry_seconds,
                }
            )

        add_audit_event(
            session=session,
            event_type="BATCH_CREATED",
            entity_type="batch",
            entity_id=str(batch.id),
            payload={
                "tenant_id": auth.tenant_id,
                "user_id": auth.user_id,
                "name": batch.name,
                "file_count": len(request.files),
            },
        )
        response = {
            "batch_id": str(batch.id),
            "status": batch.status,
            "uploads": uploads,
        }
        idempotency.success(response)
        return response


@router.post("/upload-zip/presign")
def create_zip_batch_upload(
    request: ZipBatchPresignRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, str]:
    _validate_zip_upload_metadata(mime_type=request.mime_type, size_bytes=request.size_bytes)

    storage = ObjectStorage()
    with db_session() as session:
        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="batch_zip_presign",
            idempotency_key=idempotency_key,
            request_payload=request.model_dump(),
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        batch = Batch(
            id=uuid4(),
            tenant_id=auth.tenant_id,
            user_id=auth.user_id,
            name=(request.name or "").strip() or None,
            status="ZIP_PRESIGNED",
        )
        session.add(batch)
        session.flush()

        upload_id = uuid4()
        filename = Path(request.filename).name or "batch.zip"
        object_key = f"raw-uploads/{auth.tenant_id}/batches/{batch.id}/{upload_id}/{filename}"
        upload_url = storage.create_presigned_put_url(
            bucket=settings.minio_bucket_raw,
            key=object_key,
            expires_seconds=settings.upload_url_expiry_seconds,
        )
        session.add(
            UploadSession(
                id=upload_id,
                batch_id=batch.id,
                tenant_id=auth.tenant_id,
                user_id=auth.user_id,
                filename=filename,
                mime_type=request.mime_type,
                expected_size_bytes=request.size_bytes,
                bucket_name=settings.minio_bucket_raw,
                object_key=object_key,
                status="PRESIGNED",
                expires_at=datetime.now(timezone.utc)
                + timedelta(seconds=settings.upload_url_expiry_seconds),
            )
        )
        add_audit_event(
            session=session,
            event_type="BATCH_ZIP_UPLOAD_URL_CREATED",
            entity_type="batch",
            entity_id=str(batch.id),
            payload={
                "upload_id": str(upload_id),
                "filename": filename,
                "size_bytes": request.size_bytes,
            },
        )

        response = {
            "batch_id": str(batch.id),
            "upload_id": str(upload_id),
            "upload_url": upload_url,
        }
        idempotency.success(response)
        return response


@router.post("/{batch_id}/finalise")
def finalise_batch(
    batch_id: UUID,
    request: BatchFinaliseRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    upload_ids = list(dict.fromkeys(request.upload_ids))
    if not upload_ids:
        raise HTTPException(status_code=400, detail="At least one upload_id is required")

    storage = ObjectStorage()
    with db_session() as session:
        batch = _get_batch(session=session, batch_id=batch_id, tenant_id=auth.tenant_id)
        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="batch_finalise",
            idempotency_key=idempotency_key,
            request_payload={
                "batch_id": str(batch_id),
                "upload_ids": [str(upload_id) for upload_id in upload_ids],
            },
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        document_ids: list[str] = []
        job_ids: list[str] = []
        for upload_id in upload_ids:
            upload_session = (
                session.execute(
                    select(UploadSession).where(
                        UploadSession.id == upload_id,
                        UploadSession.tenant_id == auth.tenant_id,
                        UploadSession.batch_id == batch.id,
                    )
                )
                .scalars()
                .first()
            )
            if upload_session is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Upload session {upload_id} not found for batch {batch_id}",
                )

            document, job = _finalise_upload_to_document(
                session=session,
                storage=storage,
                upload_session=upload_session,
                auth=auth,
                batch=batch,
            )
            document_ids.append(str(document.id))
            job_ids.append(str(job.id))

        batch.status = "READY"
        session.add(batch)
        add_audit_event(
            session=session,
            event_type="BATCH_FINALISED",
            entity_type="batch",
            entity_id=str(batch.id),
            payload={
                "document_ids": document_ids,
                "job_ids": job_ids,
            },
        )

        response = {
            "batch_id": str(batch.id),
            "status": batch.status,
            "document_ids": document_ids,
            "job_ids": job_ids,
        }
        idempotency.success(response)
        return response


@router.post("/{batch_id}/finalise-zip")
def finalise_zip_batch(
    batch_id: UUID,
    request: ZipBatchFinaliseRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    storage = ObjectStorage()
    with db_session() as session:
        batch = _get_batch(session=session, batch_id=batch_id, tenant_id=auth.tenant_id)
        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="batch_zip_finalise",
            idempotency_key=idempotency_key,
            request_payload={
                "batch_id": str(batch_id),
                "upload_id": str(request.upload_id),
            },
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        upload_session = (
            session.execute(
                select(UploadSession).where(
                    UploadSession.id == request.upload_id,
                    UploadSession.tenant_id == auth.tenant_id,
                    UploadSession.batch_id == batch.id,
                )
            )
            .scalars()
            .first()
        )
        if upload_session is None:
            raise HTTPException(status_code=404, detail="ZIP upload session not found")
        if upload_session.status == "FINALISED":
            raise HTTPException(status_code=409, detail="ZIP upload session already finalised")

        expires_at = upload_session.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            raise HTTPException(status_code=410, detail="Upload session has expired")

        _validate_zip_upload_metadata(
            mime_type=upload_session.mime_type,
            size_bytes=upload_session.expected_size_bytes,
        )

        try:
            zip_bytes = storage.get_bytes(
                bucket=upload_session.bucket_name,
                key=upload_session.object_key,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=409, detail="Uploaded ZIP object not found") from exc

        if len(zip_bytes) != upload_session.expected_size_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Uploaded ZIP size does not match expected size "
                    f"({len(zip_bytes)} != {upload_session.expected_size_bytes})"
                ),
            )

        accepted_files: list[dict[str, str]] = []
        rejected_files: list[dict[str, str]] = []
        total_uncompressed_bytes = 0

        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as archive_file:
                for archive_entry in archive_file.infolist():
                    filename, rejection_reason = _validate_zip_entry(
                        archive_entry=archive_entry,
                        total_uncompressed_bytes=total_uncompressed_bytes,
                        accepted_count=len(accepted_files),
                    )
                    if filename is None:
                        filename = archive_entry.filename
                    if rejection_reason is not None:
                        _reject_zip_entry(
                            rejected_files=rejected_files,
                            filename=filename,
                            reason=rejection_reason,
                        )
                        continue

                    file_bytes = archive_file.read(archive_entry)
                    total_uncompressed_bytes += len(file_bytes)
                    mime_type = ZIP_SUPPORTED_SUFFIXES[Path(filename).suffix.lower()]
                    document_id = uuid4()
                    object_uri = storage.put_bytes(
                        bucket=settings.minio_bucket_raw,
                        key=(
                            f"raw-uploads/{auth.tenant_id}/batches/{batch.id}/zip-extracted/"
                            f"{document_id}/{filename}"
                        ),
                        data=file_bytes,
                        content_type=mime_type,
                    )
                    document, _job = _create_batch_document(
                        session=session,
                        auth=auth,
                        batch=batch,
                        filename=filename,
                        mime_type=mime_type,
                        file_bytes=file_bytes,
                        storage_uri=object_uri,
                        source="zip_batch_upload",
                        document_id=document_id,
                    )
                    accepted_files.append(
                        {"filename": filename, "document_id": str(document.id)}
                    )
        except zipfile.BadZipFile as exc:
            idempotency.failure(status_code=400, detail="Invalid ZIP archive")
            raise HTTPException(status_code=400, detail="Invalid ZIP archive") from exc

        upload_session.status = "FINALISED"
        upload_session.finalised_at = datetime.now(timezone.utc)
        session.add(upload_session)

        batch.status = "READY" if accepted_files else "REJECTED"
        session.add(batch)
        add_audit_event(
            session=session,
            event_type="BATCH_ZIP_FINALISED",
            entity_type="batch",
            entity_id=str(batch.id),
            payload={
                "accepted_count": len(accepted_files),
                "rejected_count": len(rejected_files),
                "accepted_files": accepted_files,
                "rejected_files": rejected_files,
            },
        )

        response = {
            "batch_id": str(batch.id),
            "accepted_count": len(accepted_files),
            "rejected_count": len(rejected_files),
            "accepted_files": accepted_files,
            "rejected_files": rejected_files,
        }
        idempotency.success(response)
        return response


@router.post("/{batch_id}/run")
def run_batch(
    batch_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    queue = JobQueue()
    storage = ObjectStorage()
    with db_session() as session:
        batch = _get_batch(session=session, batch_id=batch_id, tenant_id=auth.tenant_id)
        batch_documents = session.execute(
            select(BatchDocument, Document)
            .join(Document, BatchDocument.document_id == Document.id)
            .where(BatchDocument.batch_id == batch.id)
            .order_by(BatchDocument.created_at.asc(), Document.created_at.asc())
        ).all()
        if not batch_documents:
            raise HTTPException(status_code=409, detail="Batch has no finalised documents")

        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="batch_run",
            idempotency_key=idempotency_key,
            request_payload={"batch_id": str(batch_id)},
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        batch.status = "RUNNING"
        session.add(batch)
        add_audit_event(
            session=session,
            event_type="BATCH_PIPELINE_RUN_STARTED",
            entity_type="batch",
            entity_id=str(batch.id),
            payload={"document_count": len(batch_documents)},
        )

        results: list[dict[str, object]] = []
        job_ids: list[str] = []
        failures = 0
        for _batch_document, document in batch_documents:
            job = _latest_job_for_document(session=session, document_id=document.id)
            if job is None:
                job = Job(
                    id=uuid4(),
                    document_id=document.id,
                    organisation_id=auth.tenant_id,
                    status="QUEUED",
                    current_stage="QUEUED",
                    queue_name=settings.processing_queue_name,
                    attempt_count=0,
                    error_message=None,
                )
                session.add(job)
                session.flush()

            queue_payload = {
                "job_id": str(job.id),
                "document_id": str(document.id),
                "tenant_id": auth.tenant_id,
                "stage": "PREPROCESSING",
                "batch_id": str(batch.id),
            }
            queue.enqueue(settings.processing_queue_name, queue_payload)
            add_audit_event(
                session=session,
                event_type="JOB_ENQUEUED",
                entity_type="job",
                entity_id=str(job.id),
                payload=queue_payload,
            )

            runner = PipelineRunner(session=session, storage=storage)
            try:
                result = runner.run(document_id=document.id)
                results.append(
                    {
                        "document_id": result.document_id,
                        "job_id": str(job.id),
                        "status": result.status,
                        "report_id": result.report_id,
                        "review_task_count": result.review_task_count,
                    }
                )
            except Exception as exc:
                failures += 1
                results.append(
                    {
                        "document_id": str(document.id),
                        "job_id": str(job.id),
                        "status": "FAILED",
                        "error": str(exc),
                        "report_id": None,
                        "review_task_count": 0,
                    }
                )
            job_ids.append(str(job.id))

        batch.status = "FAILED" if failures else "COMPLETE"
        session.add(batch)
        add_audit_event(
            session=session,
            event_type="BATCH_PIPELINE_RUN_FINISHED",
            entity_type="batch",
            entity_id=str(batch.id),
            payload={
                "status": batch.status,
                "job_ids": job_ids,
                "results": results,
                "failures": failures,
            },
        )

        response = {
            "batch_id": str(batch.id),
            "status": batch.status,
            "job_ids": job_ids,
            "results": results,
        }
        idempotency.success(response)
        return response


@router.post("/{batch_id}/reports/export")
def export_batch_report(
    batch_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    storage = ObjectStorage()
    with db_session() as session:
        batch = _get_batch(session=session, batch_id=batch_id, tenant_id=auth.tenant_id)
        documents = (
            session.execute(
                select(Document)
                .join(BatchDocument, BatchDocument.document_id == Document.id)
                .where(BatchDocument.batch_id == batch.id)
                .order_by(BatchDocument.created_at.asc(), Document.created_at.asc())
            )
            .scalars()
            .all()
        )
        if not documents:
            raise HTTPException(status_code=409, detail="Batch has no documents to export")

        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="batch_report_export",
            idempotency_key=idempotency_key,
            request_payload={"batch_id": str(batch_id)},
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        submission_period = next(
            (document.submission_period for document in documents if document.submission_period),
            None,
        )
        report = Report(
            id=uuid4(),
            document_id=None,
            batch_id=batch.id,
            submission_period=submission_period,
            output_path=None,
            status="pending",
            row_count=0,
            validation_warnings={},
        )
        session.add(report)
        session.flush()

        try:
            csv_bytes, row_count, warnings = render_report_csv(session=session, report_id=report.id)
        except ValueError as exc:
            idempotency.failure(status_code=404, detail=str(exc))
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            idempotency.failure(status_code=500, detail=str(exc))
            raise

        output_uri = storage.put_bytes(
            bucket=settings.minio_bucket_reports,
            key=f"reports/{report.id}.csv",
            data=csv_bytes,
            content_type="text/csv",
        )
        report.output_path = output_uri
        report.status = "generated"
        report.row_count = row_count
        report.validation_warnings = warnings
        session.add(report)

        add_audit_event(
            session=session,
            event_type="BATCH_REPORT_EXPORTED",
            entity_type="batch",
            entity_id=str(batch.id),
            payload={
                "report_id": str(report.id),
                "row_count": row_count,
                "warning_count": warnings.get("total_warning_count", 0),
            },
        )

        response = {
            "batch_id": str(batch.id),
            "report_id": str(report.id),
            "status": report.status,
            "row_count": row_count,
            "warning_count": warnings.get("total_warning_count", 0),
            "validation_warnings": warnings,
            "download_endpoint": f"/api/v1/reports/{report.id}/download",
        }
        idempotency.success(response)
        return response

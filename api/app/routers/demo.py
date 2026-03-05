from __future__ import annotations

import hashlib
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import Document, Job
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context
from ..services.audit import add_audit_event
from ..services.demo_sample import generate_demo_invoice_pdf
from ..services.pipeline_runner import PipelineRunner
from ..services.pipeline_state import InvalidTransitionError
from ..services.storage import ObjectStorage

router = APIRouter(prefix="/demo", tags=["demo"])


def _ensure_demo_enabled() -> None:
    if settings.environment != "local" and not settings.enable_demo_endpoints:
        raise HTTPException(status_code=404, detail="Demo endpoints are disabled")


def _create_demo_document_and_job(
    *, session: Session, storage: ObjectStorage, auth: AuthContext
) -> tuple[UUID, UUID]:
    pdf_bytes = generate_demo_invoice_pdf()
    document_id = uuid4()
    job_id = uuid4()
    filename = "demo-sample-invoice.pdf"
    object_key = f"raw-uploads/{auth.tenant_id}/{document_id}/{filename}"
    storage_uri = storage.put_bytes(
        bucket=settings.minio_bucket_raw,
        key=object_key,
        data=pdf_bytes,
        content_type="application/pdf",
    )

    checksum = hashlib.sha256(pdf_bytes).hexdigest()
    document = Document(
        id=document_id,
        organisation_id=auth.tenant_id,
        subsidiary_id="",
        organisation_size="L",
        submission_period="2025-P1",
        original_filename=filename,
        mime_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        checksum_sha256=checksum,
        uploaded_by=auth.user_id,
        storage_path=storage_uri,
        status="QUEUED",
    )
    session.add(document)

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

    add_audit_event(
        session=session,
        event_type="DEMO_SAMPLE_CREATED",
        entity_type="document",
        entity_id=str(document_id),
        payload={
            "job_id": str(job_id),
            "tenant_id": auth.tenant_id,
            "filename": filename,
            "object_key": object_key,
        },
    )

    return document_id, job_id


@router.post("/create-sample")
def create_demo_sample(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict[str, str]:
    _ensure_demo_enabled()
    with db_session() as session:
        document_id, _ = _create_demo_document_and_job(
            session=session,
            storage=ObjectStorage(),
            auth=auth,
        )
    return {"document_id": str(document_id)}


@router.post("/run")
def run_demo_pipeline(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict[str, str]:
    _ensure_demo_enabled()
    with db_session() as session:
        storage = ObjectStorage()
        document_id, job_id = _create_demo_document_and_job(
            session=session,
            storage=storage,
            auth=auth,
        )

        runner = PipelineRunner(session=session, storage=storage)
        try:
            result = runner.run(document_id=document_id)
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Pipeline failed: {exc}") from exc

    return {
        "document_id": result.document_id,
        "job_id": str(job_id),
        "report_id": result.report_id,
    }

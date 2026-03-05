from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Classification, Document, ExtractedEntity, Page, ReviewTask, TaxonomyCode
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context
from ..services.audit import add_audit_event
from ..services.pipeline_runner import PipelineRunner
from ..services.storage import ObjectStorage

router = APIRouter(prefix="/review", tags=["review"])


class FieldCorrection(BaseModel):
    field_name: str
    value: str
    page_number: int | None = None


class ClassificationCorrection(BaseModel):
    category: str
    code: str


class ReviewCorrectionRequest(BaseModel):
    extracted_fields: list[FieldCorrection] = Field(default_factory=list)
    classification_choice: ClassificationCorrection | None = None
    reviewer: str | None = None


class CompleteReviewRequest(BaseModel):
    reviewer: str | None = None


def _get_task_and_document(
    *, session: Session, task_id: UUID, tenant_id: int
) -> tuple[ReviewTask, Document]:
    row = session.execute(
        select(ReviewTask, Document)
        .join(Document, ReviewTask.document_id == Document.id)
        .where(ReviewTask.id == task_id, Document.organisation_id == tenant_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Review task not found")
    return row


@router.get("/tasks")
def list_review_tasks(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    status: str = Query(default="pending"),
) -> dict[str, list[dict]]:
    with db_session() as session:
        rows = session.execute(
            select(ReviewTask, Document)
            .join(Document, ReviewTask.document_id == Document.id)
            .where(
                Document.organisation_id == auth.tenant_id,
                ReviewTask.status == status,
            )
            .order_by(ReviewTask.created_at.asc())
        ).all()
        tasks = [
            {
                "task_id": str(task.id),
                "document_id": str(document.id),
                "task_type": task.task_type,
                "status": task.status,
                "notes": task.notes,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "filename": document.original_filename,
            }
            for task, document in rows
        ]
    return {"tasks": tasks}


@router.get("/tasks/{task_id}")
def get_review_task_detail(
    task_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict:
    with db_session() as session:
        task, document = _get_task_and_document(
            session=session, task_id=task_id, tenant_id=auth.tenant_id
        )

        pages = (
            session.execute(
                select(Page).where(Page.document_id == document.id).order_by(Page.page_number.asc())
            )
            .scalars()
            .all()
        )

        extracted_rows = (
            session.execute(
                select(ExtractedEntity)
                .where(ExtractedEntity.document_id == document.id)
                .order_by(ExtractedEntity.created_at.desc())
            )
            .scalars()
            .all()
        )

        latest_fields: dict[str, ExtractedEntity] = {}
        for row in extracted_rows:
            latest_fields.setdefault(row.field_name, row)

        classification = (
            session.execute(
                select(Classification)
                .where(Classification.document_id == document.id)
                .order_by(Classification.created_at.desc())
            )
            .scalars()
            .first()
        )

        payload = {
            "task": {
                "task_id": str(task.id),
                "task_type": task.task_type,
                "status": task.status,
                "notes": task.notes,
                "document_id": str(document.id),
            },
            "document": {
                "document_id": str(document.id),
                "filename": document.original_filename,
                "status": document.status,
            },
            "pages": [
                {
                    "page_number": page.page_number,
                    "image_endpoint": (
                        f"/api/v1/review/documents/{document.id}/pages/{page.page_number}/image"
                    ),
                    "ocr_text": page.ocr_text,
                }
                for page in pages
            ],
            "extracted_fields": [
                {
                    "field_name": row.field_name,
                    "raw_value": row.raw_value,
                    "normalized_value": row.normalized_value,
                    "confidence": float(row.confidence) if row.confidence is not None else None,
                    "page_number": row.source_page_number,
                }
                for row in latest_fields.values()
            ],
            "classification": {
                "taxonomy_category": classification.taxonomy_category if classification else None,
                "taxonomy_code": classification.taxonomy_code if classification else None,
                "confidence": (
                    float(classification.confidence)
                    if classification and classification.confidence is not None
                    else None
                ),
                "candidates": classification.candidate_codes if classification else [],
                "rule_reason": classification.rule_reason if classification else None,
            },
        }
    return payload


@router.get("/documents/{document_id}/pages/{page_number}/image")
def get_review_page_image(
    document_id: UUID,
    page_number: int,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> Response:
    with db_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.organisation_id != auth.tenant_id:
            raise HTTPException(status_code=404, detail="Document not found")

        page = (
            session.execute(
                select(Page)
                .where(Page.document_id == document_id, Page.page_number == page_number)
                .limit(1)
            )
            .scalars()
            .first()
        )
        if page is None:
            raise HTTPException(status_code=404, detail="Page not found")

        image_uri = page.normalised_image_path or page.image_path or page.raw_image_path
        if not image_uri:
            raise HTTPException(status_code=404, detail="Page image not available")

    bucket, key = ObjectStorage.parse_uri(image_uri)
    try:
        image_bytes = ObjectStorage().get_bytes(bucket=bucket, key=key)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Page image not found in object storage"
        ) from exc
    return Response(content=image_bytes, media_type="image/png")


@router.post("/tasks/{task_id}/corrections")
def submit_review_corrections(
    task_id: UUID,
    request: ReviewCorrectionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict:
    with db_session() as session:
        task, document = _get_task_and_document(
            session=session, task_id=task_id, tenant_id=auth.tenant_id
        )
        classification_override: dict[str, str] | None = None

        for correction in request.extracted_fields:
            target_page = None
            if correction.page_number is not None:
                target_page = (
                    session.execute(
                        select(Page).where(
                            Page.document_id == document.id,
                            Page.page_number == correction.page_number,
                        )
                    )
                    .scalars()
                    .first()
                )
            if target_page is None:
                target_page = (
                    session.execute(
                        select(Page)
                        .where(Page.document_id == document.id)
                        .order_by(Page.page_number.asc())
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )

            session.add(
                ExtractedEntity(
                    document_id=document.id,
                    page_id=target_page.id if target_page else None,
                    field_name=correction.field_name,
                    raw_value=correction.value,
                    normalized_value=correction.value,
                    confidence=1.0,
                    source_page_number=target_page.page_number if target_page else 1,
                    source_block_number=None,
                    source_line_number=None,
                    start_offset=None,
                    end_offset=None,
                    provenance={
                        "method": "manual_review",
                        "task_id": str(task.id),
                        "reviewer": request.reviewer or auth.user_id,
                    },
                )
            )

        if request.classification_choice is not None:
            taxonomy = (
                session.execute(
                    select(TaxonomyCode).where(
                        TaxonomyCode.active.is_(True),
                        TaxonomyCode.category == request.classification_choice.category,
                        TaxonomyCode.code == request.classification_choice.code,
                    )
                )
                .scalars()
                .first()
            )
            if taxonomy is None:
                raise HTTPException(status_code=400, detail="Invalid taxonomy code selection")
            classification_override = {
                "category": request.classification_choice.category,
                "code": request.classification_choice.code,
                "taxonomy_version": taxonomy.source_sheet,
                "reviewer": request.reviewer or auth.user_id,
            }

        task.status = "resolved"
        task.resolved_at = datetime.now(timezone.utc)
        session.add(task)
        session.flush()

        add_audit_event(
            session=session,
            event_type="REVIEW_CORRECTION_SUBMITTED",
            entity_type="review_task",
            entity_id=str(task.id),
            payload={
                "document_id": str(document.id),
                "reviewer": request.reviewer or auth.user_id,
                "field_corrections": [
                    correction.model_dump() for correction in request.extracted_fields
                ],
                "classification_choice": (
                    request.classification_choice.model_dump()
                    if request.classification_choice is not None
                    else None
                ),
            },
        )

        rerun = PipelineRunner(
            session=session, storage=ObjectStorage()
        ).rerun_downstream_from_classify(
            document_id=document.id,
            classification_override=classification_override,
            reason="review_correction",
        )
        rerun_payload = {
            "document_id": rerun.document_id,
            "status": rerun.status,
            "report_id": rerun.report_id,
            "classification_reran": rerun.classification_reran,
        }

        add_audit_event(
            session=session,
            event_type="REVIEW_TASK_COMPLETED",
            entity_type="review_task",
            entity_id=str(task.id),
            payload={"status": "resolved", "rerun": rerun_payload},
        )

    return {
        "task_id": str(task_id),
        "status": "resolved",
        "rerun": rerun_payload,
    }


@router.patch("/tasks/{task_id}/complete")
def complete_review_task(
    task_id: UUID,
    request: CompleteReviewRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict:
    with db_session() as session:
        task, document = _get_task_and_document(
            session=session, task_id=task_id, tenant_id=auth.tenant_id
        )

        task.status = "resolved"
        task.resolved_at = datetime.now(timezone.utc)
        session.add(task)
        session.flush()

        add_audit_event(
            session=session,
            event_type="REVIEW_TASK_COMPLETED",
            entity_type="review_task",
            entity_id=str(task.id),
            payload={
                "reviewer": request.reviewer or auth.user_id,
                "status": "resolved",
            },
        )

        rerun = PipelineRunner(
            session=session, storage=ObjectStorage()
        ).rerun_downstream_from_classify(
            document_id=document.id,
            classification_override=None,
            reason="review_complete",
        )
        rerun_payload = {
            "document_id": rerun.document_id,
            "status": rerun.status,
            "report_id": rerun.report_id,
            "classification_reran": rerun.classification_reran,
        }

    return {
        "task_id": str(task_id),
        "status": "resolved",
        "rerun": rerun_payload,
    }

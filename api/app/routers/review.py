from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db.models import (
    Classification,
    Document,
    DocumentMaterialClassification,
    ExtractedEntity,
    Page,
    ReviewTask,
    TaxonomyCode,
    TrainingSample,
)
from ..db.session import db_session
from ..constants import ReviewStatus, ReviewTaskType
from ..dependencies.auth import AuthContext, get_auth_context
from ..services.audit import add_audit_event
from ..services.extraction_v1 import normalize_weight_to_kg
from ..services.pipeline_runner import PipelineRunner
from ..services.storage import ObjectStorage

router = APIRouter(prefix="/review", tags=["review"])


@router.get("/material-options")
def list_material_options(
    _auth: Annotated[AuthContext, Depends(get_auth_context)],
):
    """Return the canonical list of material options for the review UI.

    This is the single source of truth — the frontend should fetch these
    rather than maintaining a hardcoded duplicate.
    """
    from ..services.material_detection import get_material_options

    return get_material_options()


class FieldCorrection(BaseModel):
    field_name: str
    value: str
    page_number: int | None = None


class ClassificationCorrection(BaseModel):
    category: str
    code: str


class MaterialCorrection(BaseModel):
    material_key: str | None = None
    material: str
    subtype: str | None = None
    taxonomy_category: str | None = "Material"
    taxonomy_code: str | None = None
    weight_value: float | None = None
    weight_unit: str | None = None
    confidence: float | None = None
    source: str | None = "review"


class ReviewCorrectionRequest(BaseModel):
    extracted_fields: list[FieldCorrection] = Field(default_factory=list)
    classification_choice: ClassificationCorrection | None = None
    materials: list[MaterialCorrection] | None = None
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


def _resolve_raw_ocr_text_span(
    *,
    session: Session,
    document_id: UUID,
    field_name: str,
    page_number: int,
    fallback_page: Page | None,
) -> tuple[str, int | None, int | None]:
    existing = (
        session.execute(
            select(ExtractedEntity)
            .where(
                ExtractedEntity.document_id == document_id,
                ExtractedEntity.field_name == field_name,
                ExtractedEntity.source_page_number == page_number,
            )
            .order_by(ExtractedEntity.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    original_candidates: list[str] = []
    if existing is not None:
        if existing.raw_value:
            original_candidates.append(existing.raw_value)
        if existing.normalized_value:
            original_candidates.append(existing.normalized_value)

    page_text = (fallback_page.ocr_text if fallback_page is not None else None) or ""
    for candidate in original_candidates:
        if not page_text:
            break
        start_idx = page_text.lower().find(candidate.lower())
        if start_idx < 0:
            continue
        end_idx = start_idx + len(candidate)
        context_start = max(0, start_idx - 150)
        context_end = min(len(page_text), end_idx + 150)
        context_text = page_text[context_start:context_end]
        return context_text, start_idx - context_start, end_idx - context_start

    if page_text:
        return page_text[:300], None, None
    if original_candidates:
        return original_candidates[0][:300], None, None
    return "", None, None


def _resolve_classification_training_context(
    *,
    session: Session,
    document_id: UUID,
) -> tuple[int, str]:
    pages = (
        session.execute(
            select(Page).where(Page.document_id == document_id).order_by(Page.page_number.asc())
        )
        .scalars()
        .all()
    )
    if not pages:
        return 1, ""

    counts: dict[int, int] = {}
    source_page_rows = session.execute(
        select(ExtractedEntity.source_page_number).where(ExtractedEntity.document_id == document_id)
    ).all()
    for (source_page_number,) in source_page_rows:
        counts[source_page_number] = counts.get(source_page_number, 0) + 1

    selected_page = max(
        pages,
        key=lambda page: (counts.get(page.page_number, 0), -page.page_number),
    )
    if selected_page.ocr_text:
        return selected_page.page_number, selected_page.ocr_text[:2000]

    combined_text = "\n".join([page.ocr_text or "" for page in pages]).strip()
    if combined_text:
        return selected_page.page_number, combined_text[:2000]

    return selected_page.page_number, ""


def _replace_weight_validation_task(
    *,
    session: Session,
    document_id: UUID,
    missing_material_keys: list[str],
) -> None:
    session.execute(
        delete(ReviewTask).where(
            ReviewTask.document_id == document_id,
            ReviewTask.task_type == ReviewTaskType.EXTRACTION_REVIEW,
            ReviewTask.status == ReviewStatus.PENDING,
            ReviewTask.notes.like("Optional: weight missing for %"),
        )
    )
    for material_key in missing_material_keys:
        session.add(
            ReviewTask(
                document_id=document_id,
                classification_id=None,
                task_type=ReviewTaskType.EXTRACTION_REVIEW,
                status=ReviewStatus.PENDING,
                notes=f"Optional: weight missing for {material_key}",
            )
        )


def _derive_material_key(*, material: str, subtype: str | None) -> str:
    material_key = material.strip()
    subtype_text = (subtype or "").strip()
    if subtype_text:
        return f"{material_key} {subtype_text}"
    return material_key


@router.get("/tasks")
def list_review_tasks(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    status: Annotated[str, Query()] = "pending",
    document_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, list[dict]]:
    with db_session() as session:
        query = (
            select(ReviewTask, Document)
            .join(Document, ReviewTask.document_id == Document.id)
            .where(
                Document.organisation_id == auth.tenant_id,
                ReviewTask.status == status,
            )
            .order_by(ReviewTask.created_at.asc())
        )
        if document_id is not None:
            query = query.where(ReviewTask.document_id == document_id)

        rows = session.execute(query).all()
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
            "materials": [
                {
                    "material_id": str(material.id),
                    "taxonomy_category": material.taxonomy_category,
                    "taxonomy_code": material.taxonomy_code,
                    "material_key": material.material_key,
                    "material": material.packaging_material,
                    "subtype": material.packaging_material_subtype,
                    "weight_value": (
                        float(material.packaging_material_weight)
                        if material.packaging_material_weight is not None
                        else None
                    ),
                    "weight_unit": material.weight_display_unit,
                    "confidence": (
                        float(material.confidence) if material.confidence is not None else None
                    ),
                    "source": material.source,
                    "created_at": material.created_at.isoformat() if material.created_at else None,
                }
                for material in (
                    session.execute(
                        select(DocumentMaterialClassification)
                        .where(DocumentMaterialClassification.document_id == document.id)
                        .order_by(DocumentMaterialClassification.created_at.asc())
                    )
                    .scalars()
                    .all()
                )
            ],
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
        reviewer = request.reviewer or auth.user_id

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

            page_number = correction.page_number or (target_page.page_number if target_page else 1)
            ocr_text, span_start, span_end = _resolve_raw_ocr_text_span(
                session=session,
                document_id=document.id,
                field_name=correction.field_name,
                page_number=page_number,
                fallback_page=target_page,
            )
            session.add(
                TrainingSample(
                    document_id=document.id,
                    page_number=page_number,
                    ocr_text=ocr_text,
                    span_start=span_start,
                    span_end=span_end,
                    corrected_value=correction.value,
                    field_name=correction.field_name,
                    source="field_correction",
                    taxonomy_code=None,
                    reviewer=reviewer,
                )
            )

            session.add(
                ExtractedEntity(
                    document_id=document.id,
                    page_id=target_page.id if target_page else None,
                    field_name=correction.field_name,
                    raw_value=correction.value,
                    normalized_value=correction.value,
                    confidence=1.0,
                    source_page_number=page_number,
                    source_block_number=None,
                    source_line_number=None,
                    start_offset=None,
                    end_offset=None,
                    provenance={
                        "method": "manual_review",
                        "task_id": str(task.id),
                        "reviewer": reviewer,
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
            context_page_number, context_text = _resolve_classification_training_context(
                session=session,
                document_id=document.id,
            )
            selected_code = request.classification_choice.code
            session.add(
                TrainingSample(
                    document_id=document.id,
                    page_number=context_page_number,
                    ocr_text=context_text,
                    span_start=None,
                    span_end=None,
                    corrected_value=selected_code,
                    field_name="taxonomy_code",
                    source="classification_override",
                    taxonomy_code=selected_code,
                    reviewer=reviewer,
                )
            )
            classification_override = {
                "category": request.classification_choice.category,
                "code": selected_code,
                "taxonomy_version": taxonomy.source_sheet,
                "reviewer": reviewer,
            }

        if request.materials is not None:
            # Validate all materials before deleting existing rows, so that
            # a validation failure does not leave the document with no materials.
            validated_materials: list[dict] = []
            missing_material_keys: list[str] = []
            for material in request.materials:
                taxonomy_category = (material.taxonomy_category or "Material").strip() or "Material"
                taxonomy_code = (material.taxonomy_code or material.material).strip()
                taxonomy = (
                    session.execute(
                        select(TaxonomyCode).where(
                            TaxonomyCode.active.is_(True),
                            TaxonomyCode.category == taxonomy_category,
                            TaxonomyCode.code == taxonomy_code,
                        )
                    )
                    .scalars()
                    .first()
                )
                if taxonomy is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Invalid taxonomy code selection for material "
                            f"{material.material}: {taxonomy_category}/{taxonomy_code}"
                        ),
                    )

                raw_weight_unit = (material.weight_unit or "").strip() or None
                weight_value = material.weight_value
                weight_unit = raw_weight_unit
                if weight_value is not None and raw_weight_unit is not None:
                    normalized = normalize_weight_to_kg(
                        str(weight_value), raw_weight_unit
                    )
                    if normalized is not None:
                        weight_value = normalized
                        weight_unit = "kg"
                material_key = (
                    material.material_key
                    if material.material_key
                    else _derive_material_key(material=material.material, subtype=material.subtype)
                )
                if weight_value is None or weight_unit is None:
                    missing_material_keys.append(material_key)

                validated_materials.append({
                    "document_id": document.id,
                    "material_key": material_key,
                    "taxonomy_category": taxonomy_category,
                    "taxonomy_code": taxonomy_code,
                    "packaging_material": material.material,
                    "packaging_material_subtype": material.subtype,
                    "packaging_material_weight": weight_value,
                    "weight_display_unit": weight_unit,
                    "confidence": material.confidence,
                    "source": (material.source or "review").strip() or "review",
                })

            # All materials validated — now atomically replace.
            session.execute(
                delete(DocumentMaterialClassification).where(
                    DocumentMaterialClassification.document_id == document.id
                )
            )
            for item in validated_materials:
                session.add(DocumentMaterialClassification(**item))

            _replace_weight_validation_task(
                session=session,
                document_id=document.id,
                missing_material_keys=missing_material_keys,
            )
            add_audit_event(
                session=session,
                event_type="DOCUMENT_MATERIALS_UPDATED",
                entity_type="document",
                entity_id=str(document.id),
                payload={
                    "task_id": str(task.id),
                    "reviewer": reviewer,
                    "material_count": len(request.materials),
                    "missing_weight_count": len(missing_material_keys),
                    "materials": [item.model_dump() for item in request.materials],
                },
            )

        task.status = ReviewStatus.RESOLVED
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
                "reviewer": reviewer,
                "field_corrections": [
                    correction.model_dump() for correction in request.extracted_fields
                ],
                "classification_choice": (
                    request.classification_choice.model_dump()
                    if request.classification_choice is not None
                    else None
                ),
                "materials": (
                    [item.model_dump() for item in request.materials]
                    if request.materials is not None
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

        task.status = ReviewStatus.RESOLVED
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

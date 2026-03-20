from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..constants import DocumentStatus, MaterialSource, ReportStatus, ReviewStatus, ReviewTaskType
from ..db.models import (
    Classification,
    Document,
    DocumentMaterialClassification,
    Entity,
    ExtractedEntity,
    Job,
    Page,
    Report,
    ReviewTask,
)
from .audit import add_audit_event
from .classification_v1 import ClassificationServiceV1
from .document_insights import (
    DOCUMENT_TYPE_NOTICE_OF_LIABILITY,
    DocumentInsightsService,
)
from .extraction_v1 import ExtractionV1Service, normalize_weight_to_kg
from .logging_utils import log_json
from .material_detection import detect_materials
from .ocr import OCRService
from .pipeline_state import validate_transition
from .preprocess import PreprocessService
from .report_export import render_report_csv
from .storage import ObjectStorage
from .tenant_settings import is_tenant_ner_enabled

T = TypeVar("T")


@dataclass
class PipelineRunResult:
    document_id: str
    status: str
    report_id: str
    review_task_count: int


@dataclass
class PipelineRerunResult:
    document_id: str
    status: str
    report_id: str
    classification_reran: bool


@dataclass
class StageExecutionError(RuntimeError):
    stage: str
    attempts: int
    reason: str

    def __str__(self) -> str:
        return f"{self.stage} failed after {self.attempts} attempts: {self.reason}"


class PipelineRunner:
    _OCR_REVIEW_STOPWORDS = {"invoice", "bill", "to", "date"}
    _REQUIRED_FIELD_LINE_HINTS = (
        "invoice ref",
        "invoice no",
        "invoice number",
        "invoice date",
        "bill to",
        "supplier",
        "product",
        "description",
        "weight",
        "qty",
        "quantity",
        "ref",
    )
    _PUNCTUATION_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
    _NUMERIC_TOKEN_RE = re.compile(r"^\d+(?:[.,]\d+)?$")

    def __init__(self, *, session: Session, storage: ObjectStorage) -> None:
        self.session = session
        self.storage = storage
        self.logger = logging.getLogger("packtrack.pipeline")
        self._job_id: str | None = None

    def run(self, *, document_id: UUID) -> PipelineRunResult:
        document = self.session.get(Document, document_id)
        if document is None:
            raise ValueError(f"Document {document_id} not found")
        self._job_id = self._get_job_id(document=document)

        report: Report | None = None
        try:
            self._log(
                level="info",
                event="pipeline_run_started",
                message="Pipeline run started",
                document_id=str(document.id),
                state=document.status,
            )
            self._audit(document, "PIPELINE_RUN_STARTED", {"state": document.status})

            self._transition(document, "PREPROCESSING")
            preprocess_output = self._run_stage_with_retries(
                document=document,
                stage_name="PREPROCESSING",
                stage_runner=lambda: self._run_preprocess(document),
            )

            self._transition(document, "EXTRACTING")
            extract_output = self._run_stage_with_retries(
                document=document,
                stage_name="EXTRACTING",
                stage_runner=lambda: self._run_extract(document),
            )

            self._transition(document, "CLASSIFYING")
            classify_output = self._run_stage_with_retries(
                document=document,
                stage_name="CLASSIFYING",
                stage_runner=lambda: self._run_classify(document),
            )

            self._transition(document, "REPORTING")
            report = self._run_stage_with_retries(
                document=document,
                stage_name="REPORTING",
                stage_runner=lambda: self._run_reporting(document),
            )

            self._transition(document, "COMPLETE")
            self._audit(
                document,
                "PIPELINE_RUN_COMPLETED",
                {
                    "preprocess": preprocess_output,
                    "extract": extract_output,
                    "classify": classify_output,
                    "report_id": str(report.id),
                },
            )
        except Exception as exc:
            failed_reason = str(exc)
            document.status = DocumentStatus.FAILED
            self.session.add(document)
            self._sync_job_state(document=document, state="FAILED", error_message=failed_reason)
            self._audit(document, "PIPELINE_RUN_FAILED", {"error": failed_reason})
            self._log(
                level="error",
                event="pipeline_run_failed",
                message="Pipeline run failed",
                document_id=str(document.id),
                error=failed_reason,
            )
            raise

        self.session.flush()
        review_task_count = (
            self.session.execute(select(ReviewTask).where(ReviewTask.document_id == document.id))
            .scalars()
            .all()
        )

        if report is None:
            raise RuntimeError("Report was not generated")

        self._log(
            level="info",
            event="pipeline_run_completed",
            message="Pipeline run completed",
            document_id=str(document.id),
            report_id=str(report.id),
            review_task_count=len(review_task_count),
        )
        return PipelineRunResult(
            document_id=str(document.id),
            status=document.status,
            report_id=str(report.id),
            review_task_count=len(review_task_count),
        )

    def rerun_downstream_from_classify(
        self,
        *,
        document_id: UUID,
        classification_override: dict[str, str] | None = None,
        reason: str = "review_correction",
    ) -> PipelineRerunResult:
        document = self.session.get(Document, document_id)
        if document is None:
            raise ValueError(f"Document {document_id} not found")
        self._job_id = self._get_job_id(document=document)

        report: Report | None = None
        classification_reran = True
        self._audit(
            document,
            "PIPELINE_RERUN_REQUESTED",
            {
                "from_stage": "CLASSIFYING",
                "previous_status": document.status,
                "classification_override": classification_override,
                "reason": reason,
            },
        )

        try:
            self._force_transition(document=document, next_state="CLASSIFYING", reason=reason)
            if classification_override is not None:
                self._run_stage_with_retries(
                    document=document,
                    stage_name="CLASSIFYING",
                    stage_runner=lambda: self._run_manual_classify(
                        document=document,
                        category=classification_override["category"],
                        code=classification_override["code"],
                        taxonomy_version=classification_override["taxonomy_version"],
                        reviewer=classification_override.get("reviewer"),
                    ),
                )
            else:
                self._run_stage_with_retries(
                    document=document,
                    stage_name="CLASSIFYING",
                    stage_runner=lambda: self._run_classify(document),
                )

            self._force_transition(document=document, next_state="REPORTING", reason=reason)
            report = self._run_stage_with_retries(
                document=document,
                stage_name="REPORTING",
                stage_runner=lambda: self._run_reporting(document),
            )

            self._force_transition(document=document, next_state="COMPLETE", reason=reason)
            self._audit(
                document,
                "PIPELINE_RERUN_COMPLETED",
                {
                    "classification_reran": classification_reran,
                    "report_id": str(report.id),
                },
            )
        except Exception as exc:
            failed_reason = str(exc)
            document.status = DocumentStatus.FAILED
            self.session.add(document)
            self._sync_job_state(document=document, state="FAILED", error_message=failed_reason)
            self._audit(
                document,
                "PIPELINE_RERUN_FAILED",
                {"error": failed_reason, "classification_reran": classification_reran},
            )
            self._log(
                level="error",
                event="pipeline_rerun_failed",
                message="Pipeline rerun failed",
                document_id=str(document.id),
                reason=reason,
                error=failed_reason,
            )
            raise

        if report is None:
            raise RuntimeError("Report was not generated during rerun")

        return PipelineRerunResult(
            document_id=str(document.id),
            status=document.status,
            report_id=str(report.id),
            classification_reran=classification_reran,
        )

    def _transition(self, document: Document, next_state: str) -> None:
        validate_transition(document.status, next_state)
        self._audit(document, "STAGE_STARTED", {"from": document.status, "to": next_state})
        self._log(
            level="info",
            event="stage_transition_started",
            message="Pipeline stage transition started",
            document_id=str(document.id),
            stage_from=document.status,
            stage_to=next_state,
        )
        document.status = next_state
        self.session.add(document)
        self._sync_job_state(document=document, state=next_state, error_message=None)
        self._audit(document, "STAGE_FINISHED", {"state": next_state})
        self._log(
            level="info",
            event="stage_transition_finished",
            message="Pipeline stage transition finished",
            document_id=str(document.id),
            stage=next_state,
        )

    def _force_transition(self, *, document: Document, next_state: str, reason: str) -> None:
        current_state = document.status
        self._audit(
            document,
            "STAGE_STARTED",
            {"from": current_state, "to": next_state, "mode": "rerun", "reason": reason},
        )
        self._log(
            level="info",
            event="stage_transition_started",
            message="Pipeline stage transition started",
            document_id=str(document.id),
            stage_from=current_state,
            stage_to=next_state,
            mode="rerun",
            reason=reason,
        )
        document.status = next_state
        self.session.add(document)
        self._sync_job_state(document=document, state=next_state, error_message=None)
        self._audit(
            document,
            "STAGE_FINISHED",
            {"state": next_state, "mode": "rerun", "reason": reason},
        )
        self._log(
            level="info",
            event="stage_transition_finished",
            message="Pipeline stage transition finished",
            document_id=str(document.id),
            stage=next_state,
            mode="rerun",
            reason=reason,
        )

    def _run_stage_with_retries(
        self,
        *,
        document: Document,
        stage_name: str,
        stage_runner: Callable[[], T],
    ) -> T:
        max_attempts = max(1, settings.pipeline_stage_max_attempts)
        for attempt in range(1, max_attempts + 1):
            self._audit(
                document,
                "STAGE_ATTEMPT_STARTED",
                {"stage": stage_name, "attempt": attempt, "max_attempts": max_attempts},
            )
            self._log(
                level="info",
                event="stage_attempt_started",
                message="Pipeline stage attempt started",
                document_id=str(document.id),
                stage=stage_name,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            try:
                result = stage_runner()
            except Exception as exc:
                self._increment_job_attempt(document=document)
                self._audit(
                    document,
                    "STAGE_ATTEMPT_FAILED",
                    {
                        "stage": stage_name,
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "error": str(exc),
                    },
                )
                self._log(
                    level="error",
                    event="stage_attempt_failed",
                    message="Pipeline stage attempt failed",
                    document_id=str(document.id),
                    stage=stage_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error=str(exc),
                )
                if attempt < max_attempts:
                    self._audit(
                        document,
                        "STAGE_RETRY_SCHEDULED",
                        {
                            "stage": stage_name,
                            "next_attempt": attempt + 1,
                            "max_attempts": max_attempts,
                        },
                    )
                    continue
                raise StageExecutionError(
                    stage=stage_name,
                    attempts=max_attempts,
                    reason=str(exc),
                ) from exc

            self._audit(
                document,
                "STAGE_ATTEMPT_FINISHED",
                {"stage": stage_name, "attempt": attempt, "max_attempts": max_attempts},
            )
            self._log(
                level="info",
                event="stage_attempt_finished",
                message="Pipeline stage attempt finished",
                document_id=str(document.id),
                stage=stage_name,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            return result
        raise StageExecutionError(
            stage=stage_name,
            attempts=max(1, settings.pipeline_stage_max_attempts),
            reason="unknown stage runner failure",
        )

    def _run_preprocess(self, document: Document) -> dict[str, Any]:
        self._audit(document, "PREPROCESS_STAGE_STARTED", {})
        self.session.execute(delete(Page).where(Page.document_id == document.id))
        self.session.flush()
        preprocess_service = PreprocessService(storage=self.storage)
        pages = preprocess_service.preprocess_document(document=document)

        for page in pages:
            page_row = Page(
                document_id=document.id,
                page_number=page.page_number,
                page_width=page.width,
                page_height=page.height,
                raw_image_path=page.raw_image_uri,
                normalised_image_path=page.normalised_image_uri,
                processing_time_ms=page.processing_ms,
                image_path=page.normalised_image_uri,
                ocr_text=None,
            )
            self.session.add(page_row)
            self._audit(
                document,
                "PREPROCESS_PAGE_PROCESSED",
                {
                    "page_number": page.page_number,
                    "width": page.width,
                    "height": page.height,
                    "raw_image_uri": page.raw_image_uri,
                    "normalised_image_uri": page.normalised_image_uri,
                    "processing_time_ms": page.processing_ms,
                },
            )

        self.session.flush()
        output = {
            "page_count": len(pages),
            "pages": [
                {
                    "page_number": page.page_number,
                    "width": page.width,
                    "height": page.height,
                    "normalised_image_uri": page.normalised_image_uri,
                    "processing_time_ms": page.processing_ms,
                }
                for page in pages
            ],
        }
        self._audit(document, "PREPROCESS_STAGE_FINISHED", output)
        return output

    def _run_extract(self, document: Document) -> dict[str, Any]:
        self._audit(document, "EXTRACT_STAGE_STARTED", {})
        pages = (
            self.session.execute(
                select(Page).where(Page.document_id == document.id).order_by(Page.page_number.asc())
            )
            .scalars()
            .all()
        )
        if not pages:
            raise RuntimeError("Page records missing for extract stage")
        page_ids = [page.id for page in pages]
        if page_ids:
            self.session.execute(delete(Entity).where(Entity.page_id.in_(page_ids)))
        self.session.execute(
            delete(ExtractedEntity).where(ExtractedEntity.document_id == document.id)
        )
        self.session.execute(
            delete(DocumentMaterialClassification).where(
                DocumentMaterialClassification.document_id == document.id,
                DocumentMaterialClassification.source == "auto",
            )
        )
        self.session.execute(
            delete(ReviewTask).where(
                ReviewTask.document_id == document.id,
                ReviewTask.task_type.in_(["OCR_REVIEW", "EXTRACTION_REVIEW"]),
                ReviewTask.status == ReviewStatus.PENDING,
            )
        )
        self.session.flush()

        ocr_service = OCRService(storage=self.storage)
        tenant_ner_enabled = False
        if document.organisation_id is not None:
            tenant_ner_enabled = is_tenant_ner_enabled(
                session=self.session,
                tenant_id=document.organisation_id,
            )
        extraction_service = ExtractionV1Service(tenant_ner_enabled=tenant_ner_enabled)
        if extraction_service.ner_model_registry is not None:
            registry = extraction_service.ner_model_registry
            document.ner_model_path = registry.model_path
            document.ner_model_trained_at = registry.trained_at
            document.ner_model_f1 = registry.overall_f1
            self.session.add(document)

            job = self._get_latest_job(document=document)
            if job is not None:
                job.ner_model_path = registry.model_path
                job.ner_model_trained_at = registry.trained_at
                job.ner_model_f1 = registry.overall_f1
                self.session.add(job)

            self._audit(
                document,
                "NER_MODEL_USED",
                {
                    "model_path": registry.model_path,
                    "trained_at": registry.trained_at.isoformat(),
                    "overall_f1": registry.overall_f1,
                    "per_label_f1": registry.per_label_f1,
                    "labels": registry.labels,
                },
            )
        all_items: list[dict[str, Any]] = []
        page_summaries: list[dict[str, Any]] = []
        extracted_candidates = []
        page_texts: list[str] = []

        for page in pages:
            image_uri = page.normalised_image_path or page.image_path
            if not image_uri:
                raise RuntimeError(f"Page {page.page_number} has no normalised image path")

            page_result = ocr_service.process_page(
                document_id=document.id,
                page_number=page.page_number,
                image_uri=image_uri,
            )
            page.ocr_text = page_result.raw_text
            self.session.add(page)
            page_texts.append(page_result.raw_text)

            line_text_by_location = {
                (item.block_number, item.line_number): item.text
                for item in page_result.items
                if item.item_type == "line"
            }
            low_confidence_tokens = []
            for item in page_result.items:
                label = {
                    "block": "OCR_BLOCK",
                    "line": "OCR_LINE",
                    "token": "OCR_TOKEN",
                }[item.item_type]
                entity = Entity(
                    page_id=page.id,
                    label=label,
                    text=item.text,
                    confidence=item.confidence,
                    entity_metadata={
                        "source": "tesseract",
                        "item_type": item.item_type,
                        "bbox": item.bbox,
                        "page_number": item.page_number,
                        "block_number": item.block_number,
                        "line_number": item.line_number,
                        "token_number": item.token_number,
                    },
                )
                self.session.add(entity)
                all_items.append({"label": label, "text": item.text, "confidence": item.confidence})

                if (
                    item.item_type == "token"
                    and item.confidence < settings.ocr_confidence_threshold
                    and not self._is_noise_low_conf_token(
                        token_text=item.text,
                        line_text=line_text_by_location.get(
                            (item.block_number, item.line_number),
                            "",
                        ),
                    )
                ):
                    low_confidence_tokens.append(item)

            if low_confidence_tokens:
                ocr_review_summary = self._build_ocr_review_summary(
                    page_number=page.page_number,
                    threshold=settings.ocr_confidence_threshold,
                    tokens=low_confidence_tokens,
                    artifact_uri=page_result.artifact_tsv_uri,
                )
                self.session.add(
                    ReviewTask(
                        document_id=document.id,
                        classification_id=None,
                        task_type="OCR_REVIEW",
                        status=ReviewStatus.PENDING,
                        notes=json.dumps(ocr_review_summary, ensure_ascii=True),
                    )
                )
                self._audit(
                    document,
                    "REVIEW_TASK_CREATED",
                    {
                        "task_type": "OCR_REVIEW",
                        **ocr_review_summary,
                    },
                )

            extraction_result = extraction_service.extract_from_page(
                page_number=page.page_number,
                page_text=page_result.raw_text,
                ocr_items=page_result.items,
            )
            for candidate in extraction_result.candidates:
                extracted_candidates.append(candidate)
                self.session.add(
                    ExtractedEntity(
                        document_id=document.id,
                        page_id=page.id,
                        field_name=candidate.field_name,
                        raw_value=candidate.raw_value,
                        normalized_value=candidate.normalized_value,
                        confidence=candidate.confidence,
                        source_page_number=candidate.source_page_number,
                        source_block_number=candidate.source_block_number,
                        source_line_number=candidate.source_line_number,
                        start_offset=candidate.start_offset,
                        end_offset=candidate.end_offset,
                        provenance=candidate.provenance,
                    )
                )

            page_summary = {
                "page_number": page.page_number,
                "text_uri": page_result.artifact_text_uri,
                "tsv_uri": page_result.artifact_tsv_uri,
                "hocr_uri": page_result.artifact_hocr_uri,
                "item_count": len(page_result.items),
                "low_confidence_items": len(low_confidence_tokens),
                "extracted_fields": len(extraction_result.candidates),
            }
            page_summaries.append(page_summary)
            self._audit(document, "OCR_PAGE_PROCESSED", page_summary)

        document_insights = DocumentInsightsService().inspect(page_texts=page_texts)
        document.document_type = document_insights.document_type
        document.document_date = document_insights.document_date
        document.inferred_country_code = document_insights.inferred_country_code
        document.country_inference_source = document_insights.country_inference_source
        self.session.add(document)

        self._audit(
            document,
            "DOCUMENT_METADATA_EXTRACTED",
            {
                "document_type": document.document_type,
                "document_date": document.document_date,
                "inferred_country_code": document.inferred_country_code,
                "country_inference_source": document.country_inference_source,
            },
        )

        persisted_materials: list[dict[str, Any]] = []
        if document_insights.material_rows:
            for structured_material in document_insights.material_rows:
                # Canonical weight: always store in kg.
                weight_kg = None
                if (
                    structured_material.weight_value is not None
                    and structured_material.weight_unit
                ):
                    weight_kg = normalize_weight_to_kg(
                        str(structured_material.weight_value),
                        structured_material.weight_unit,
                    )
                    if weight_kg is None:
                        weight_kg = float(structured_material.weight_value)

                self.session.add(
                    DocumentMaterialClassification(
                        document_id=document.id,
                        material_key=structured_material.material_key,
                        taxonomy_category="Material",
                        taxonomy_code=structured_material.packaging_material,
                        packaging_material=structured_material.packaging_material,
                        packaging_material_subtype=structured_material.packaging_material_subtype,
                        packaging_material_weight=weight_kg,
                        weight_display_unit="kg" if weight_kg is not None else None,
                        confidence=structured_material.confidence,
                        source=structured_material.source,
                    )
                )
                persisted_materials.append(
                    {
                        "material_key": structured_material.material_key,
                        "packaging_material": structured_material.packaging_material,
                        "packaging_material_subtype": (
                            structured_material.packaging_material_subtype
                        ),
                        "packaging_material_weight": (
                            None if weight_kg is None else str(weight_kg)
                        ),
                        "weight_display_unit": "kg" if weight_kg is not None else None,
                        "original_unit": structured_material.weight_unit,
                        "original_value": (
                            None
                            if structured_material.weight_value is None
                            else str(structured_material.weight_value)
                        ),
                        "confidence": structured_material.confidence,
                        "source": structured_material.source,
                        "provenance": structured_material.provenance,
                    }
                )
        else:
            auto_materials = detect_materials(
                page_texts=page_texts,
                extracted_candidates=extracted_candidates,
            )
            for detected_material in auto_materials:
                self.session.add(
                    DocumentMaterialClassification(
                        document_id=document.id,
                        material_key=detected_material.material_key,
                        taxonomy_category="Material",
                        taxonomy_code=detected_material.packaging_material,
                        packaging_material=detected_material.packaging_material,
                        packaging_material_subtype=detected_material.packaging_material_subtype,
                        packaging_material_weight=None,
                        weight_display_unit=None,
                        confidence=detected_material.confidence,
                        source=detected_material.source,
                    )
                )
                persisted_materials.append(
                    {
                        "material_key": detected_material.material_key,
                        "packaging_material": detected_material.packaging_material,
                        "packaging_material_subtype": detected_material.packaging_material_subtype,
                        "packaging_material_weight": None,
                        "weight_display_unit": None,
                        "confidence": detected_material.confidence,
                        "source": detected_material.source,
                        "provenance": {"method": "keyword_rules"},
                    }
                )

        if persisted_materials:
            self._audit(
                document,
                "MATERIALS_AUTO_DETECTED",
                {
                    "materials": persisted_materials,
                },
            )

        weight_value_candidates = [
            candidate
            for candidate in extracted_candidates
            if candidate.field_name == "weight_value"
        ]
        weight_unit_candidates = [
            candidate for candidate in extracted_candidates if candidate.field_name == "weight_unit"
        ]
        has_weight_values = bool(weight_value_candidates)
        has_weight_units = bool(weight_unit_candidates)
        low_confidence_materials = [
            item
            for item in persisted_materials
            if float(item["confidence"]) < settings.classification_confidence_threshold
        ]
        for detected_material in persisted_materials:
            if (
                detected_material["packaging_material_weight"]
                and detected_material["weight_display_unit"]
            ):
                continue
            if not has_weight_values or not has_weight_units:
                self.session.add(
                    ReviewTask(
                        document_id=document.id,
                        classification_id=None,
                        task_type=ReviewTaskType.EXTRACTION_REVIEW,
                        status=ReviewStatus.PENDING,
                        notes=f"Optional: weight missing for {detected_material['material_key']}",
                    )
                )
        for detected_material in low_confidence_materials:
            self.session.add(
                ReviewTask(
                    document_id=document.id,
                    classification_id=None,
                    task_type=ReviewTaskType.EXTRACTION_REVIEW,
                    status=ReviewStatus.PENDING,
                    notes=(
                        "Optional: low confidence material detection for "
                        f"{detected_material['material_key']} "
                        f"({float(detected_material['confidence']):.2f})"
                    ),
                )
            )

        missing_fields, ambiguous_fields = extraction_service.build_review_findings(
            candidates=extracted_candidates
        )
        if document_insights.document_date and "invoice_date" in missing_fields:
            missing_fields = [field for field in missing_fields if field != "invoice_date"]
        if document_insights.document_type == DOCUMENT_TYPE_NOTICE_OF_LIABILITY:
            suppressed_notice_fields = {
                "invoice_ref",
                "invoice_date",
                "supplier_ref_or_name",
                "product_desc",
                "weight_value",
                "weight_unit",
            }
            missing_fields = [
                field for field in missing_fields if field not in suppressed_notice_fields
            ]
            ambiguous_fields = [
                field for field in ambiguous_fields if field not in suppressed_notice_fields
            ]
        for field_name in missing_fields:
            if persisted_materials and field_name in {"weight_value", "weight_unit"}:
                continue
            self.session.add(
                ReviewTask(
                    document_id=document.id,
                    classification_id=None,
                    task_type=ReviewTaskType.EXTRACTION_REVIEW,
                    status=ReviewStatus.PENDING,
                    notes=f"Required extracted field is missing: {field_name}",
                )
            )
            self._audit(
                document,
                "REVIEW_TASK_CREATED",
                {
                    "task_type": "EXTRACTION_REVIEW",
                    "reason": "missing_field",
                    "field": field_name,
                },
            )
        for field_name in ambiguous_fields:
            self.session.add(
                ReviewTask(
                    document_id=document.id,
                    classification_id=None,
                    task_type=ReviewTaskType.EXTRACTION_REVIEW,
                    status=ReviewStatus.PENDING,
                    notes=f"Extracted field is ambiguous: {field_name}",
                )
            )
            self._audit(
                document,
                "REVIEW_TASK_CREATED",
                {
                    "task_type": "EXTRACTION_REVIEW",
                    "reason": "ambiguous_field",
                    "field": field_name,
                },
            )

        self.session.flush()

        extract_output = {
            "page_count": len(page_summaries),
            "threshold": settings.ocr_confidence_threshold,
            "pages": page_summaries,
            "entity_count": len(all_items),
            "extracted_entity_count": len(extracted_candidates),
            "document_type": document.document_type,
            "document_date": document.document_date,
            "inferred_country_code": document.inferred_country_code,
            "document_insight_warnings": document_insights.warnings,
            "auto_material_count": len(persisted_materials),
            "materials": persisted_materials,
            "missing_fields": missing_fields,
            "ambiguous_fields": ambiguous_fields,
        }
        extract_key = f"ocr-output/{document.id}/extract-summary.json"
        extract_uri = self.storage.put_bytes(
            bucket=settings.minio_bucket_raw,
            key=extract_key,
            data=json.dumps(extract_output).encode("utf-8"),
            content_type="application/json",
        )

        self._audit(
            document, "EXTRACT_STAGE_FINISHED", {"artifact_uri": extract_uri, **extract_output}
        )
        return extract_output

    def _run_classify(self, document: Document) -> dict[str, Any]:
        self._audit(document, "CLASSIFY_STAGE_STARTED", {})
        self.session.execute(
            delete(Classification).where(Classification.document_id == document.id)
        )
        self.session.execute(
            delete(ReviewTask).where(
                ReviewTask.document_id == document.id,
                ReviewTask.task_type == ReviewTaskType.CLASSIFICATION_REVIEW,
                ReviewTask.status == ReviewStatus.PENDING,
            )
        )
        self.session.flush()
        decision = ClassificationServiceV1(session=self.session).classify_document(
            document_id=document.id
        )
        inferred_country_code = document.inferred_country_code or ""
        classification = Classification(
            document_id=document.id,
            row_index=1,
            taxonomy_category=decision.taxonomy_category,
            taxonomy_code=decision.taxonomy_code,
            taxonomy_version=decision.taxonomy_version,
            packaging_activity=decision.packaging_activity,
            packaging_type=decision.packaging_type,
            packaging_class=decision.packaging_class,
            packaging_material=decision.packaging_material,
            packaging_material_subtype=decision.packaging_material_subtype,
            from_country=inferred_country_code,
            to_country=inferred_country_code,
            packaging_material_weight=decision.packaging_material_weight,
            packaging_material_units=None,
            transitional_packaging_units=None,
            ram_rag_rating="",
            confidence=decision.confidence,
            candidate_codes=decision.candidates,
            rule_id=decision.rule_id,
            rule_reason=decision.reason,
            source="rules",
        )
        self.session.add(classification)
        self.session.flush()

        if decision.confidence < settings.classification_confidence_threshold:
            task = ReviewTask(
                document_id=document.id,
                classification_id=classification.id,
                task_type=ReviewTaskType.CLASSIFICATION_REVIEW,
                status=ReviewStatus.PENDING,
                notes=(
                    f"Classification confidence below threshold ({decision.confidence} < "
                    f"{settings.classification_confidence_threshold})"
                ),
            )
            self.session.add(task)
            self._audit(
                document,
                "REVIEW_TASK_CREATED",
                {
                    "task_type": "CLASSIFICATION_REVIEW",
                    "confidence": decision.confidence,
                    "threshold": settings.classification_confidence_threshold,
                    "candidates": decision.candidates,
                },
            )
        self.session.flush()

        classify_output = {
            "packaging_activity": decision.packaging_activity,
            "packaging_type": decision.packaging_type,
            "packaging_class": decision.packaging_class,
            "packaging_material": decision.packaging_material,
            "confidence": decision.confidence,
            "taxonomy_version": decision.taxonomy_version,
            "rule_id": decision.rule_id,
            "reason": decision.reason,
            "candidates": decision.candidates,
        }
        classify_key = f"classify-output/{document.id}/classification.json"
        classify_uri = self.storage.put_bytes(
            bucket=settings.minio_bucket_raw,
            key=classify_key,
            data=json.dumps(classify_output).encode("utf-8"),
            content_type="application/json",
        )

        decision_event = "CLASSIFICATION_REVIEW_REQUIRED"
        self._audit(
            document,
            decision_event,
            {
                "artifact_uri": classify_uri,
                "confidence": decision.confidence,
                "threshold": settings.classification_confidence_threshold,
                "candidates": decision.candidates,
            },
        )
        self._audit(document, "CLASSIFY_STAGE_FINISHED", {"artifact_uri": classify_uri})
        return classify_output

    def _run_reporting(self, document: Document) -> Report:
        self._audit(document, "REPORT_STAGE_STARTED", {})

        report = (
            self.session.execute(
                select(Report)
                .where(Report.document_id == document.id, Report.status.in_(["pending", "failed"]))
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if report is None:
            report = Report(
                document_id=document.id,
                submission_period=document.submission_period,
                output_path=None,
                status=ReviewStatus.PENDING,
                row_count=0,
            )
            self.session.add(report)
            self.session.flush()
        else:
            report.submission_period = document.submission_period
            report.output_path = None
            report.status = ReportStatus.PENDING
            report.row_count = 0
            self.session.add(report)
            self.session.flush()

        csv_bytes, row_count, warnings = render_report_csv(
            session=self.session,
            report_id=report.id,
        )
        report_key = f"reports/{report.id}.csv"
        report_uri = self.storage.put_bytes(
            bucket=settings.minio_bucket_reports,
            key=report_key,
            data=csv_bytes,
            content_type="text/csv",
        )

        report.output_path = report_uri
        report.status = ReportStatus.GENERATED
        report.row_count = row_count
        report.validation_warnings = warnings
        self.session.add(report)
        self.session.flush()

        warning_count = len(warnings.get("overall", []))
        self._audit(
            document,
            "REPORT_WARNINGS_GENERATED",
            {
                "report_id": str(report.id),
                "warning_count": warning_count,
                "missing_fields_by_row": warnings.get("missing_fields_by_row", []),
            },
        )

        self._audit(
            document,
            "REPORT_STAGE_FINISHED",
            {
                "report_id": str(report.id),
                "row_count": row_count,
                "output_uri": report_uri,
                "warning_count": warning_count,
            },
        )
        return report

    def _run_manual_classify(
        self,
        *,
        document: Document,
        category: str,
        code: str,
        taxonomy_version: str,
        reviewer: str | None,
    ) -> dict[str, Any]:
        self._audit(document, "CLASSIFY_STAGE_STARTED", {"mode": "manual_override"})
        previous = (
            self.session.execute(
                select(Classification)
                .where(Classification.document_id == document.id)
                .order_by(Classification.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

        classification = Classification(
            document_id=document.id,
            row_index=1,
            taxonomy_category=category,
            taxonomy_code=code,
            taxonomy_version=taxonomy_version,
            packaging_activity=previous.packaging_activity if previous else "",
            packaging_type=previous.packaging_type if previous else "",
            packaging_class=previous.packaging_class if previous else "",
            packaging_material=previous.packaging_material if previous else "",
            packaging_material_subtype=previous.packaging_material_subtype if previous else "",
            from_country=previous.from_country if previous else "",
            to_country=previous.to_country if previous else "",
            packaging_material_weight=previous.packaging_material_weight if previous else None,
            packaging_material_units=previous.packaging_material_units if previous else None,
            transitional_packaging_units=previous.transitional_packaging_units
            if previous
            else None,
            ram_rag_rating=previous.ram_rag_rating if previous else "",
            confidence=0.99,
            candidate_codes=[
                {
                    "category": category,
                    "code": code,
                    "score": 1.0,
                    "rule_id": "manual.review",
                    "reason": "manual review selection",
                }
            ],
            rule_id="manual.review",
            rule_reason=f"manual review correction by {reviewer or 'unknown'}",
            source="human",
        )

        if category == "Material":
            classification.packaging_material = code
        if category == "Packaging Activity":
            classification.packaging_activity = code
        if category == "Packaging Type":
            classification.packaging_type = code
        if category == "Packaging Class":
            classification.packaging_class = code

        self.session.add(classification)
        self.session.flush()

        classify_output = {
            "packaging_activity": classification.packaging_activity,
            "packaging_type": classification.packaging_type,
            "packaging_class": classification.packaging_class,
            "packaging_material": classification.packaging_material,
            "confidence": 0.99,
            "taxonomy_version": taxonomy_version,
            "rule_id": "manual.review",
            "reason": classification.rule_reason,
            "candidates": classification.candidate_codes,
        }
        classify_key = f"classify-output/{document.id}/classification-manual.json"
        classify_uri = self.storage.put_bytes(
            bucket=settings.minio_bucket_raw,
            key=classify_key,
            data=json.dumps(classify_output).encode("utf-8"),
            content_type="application/json",
        )

        self._audit(
            document,
            "CLASSIFY_MANUAL_OVERRIDE_APPLIED",
            {
                "category": category,
                "code": code,
                "taxonomy_version": taxonomy_version,
                "reviewer": reviewer,
                "artifact_uri": classify_uri,
            },
        )
        self._audit(
            document,
            "CLASSIFY_STAGE_FINISHED",
            {"artifact_uri": classify_uri, "mode": "manual_override"},
        )
        return classify_output

    def _audit(self, document: Document, event_type: str, payload: dict[str, Any]) -> None:
        payload_with_job = dict(payload)
        if self._job_id is not None:
            payload_with_job.setdefault("job_id", self._job_id)
        add_audit_event(
            session=self.session,
            event_type=event_type,
            entity_type="document",
            entity_id=str(document.id),
            payload=payload_with_job,
        )

    def _sync_job_state(
        self,
        *,
        document: Document,
        state: str,
        error_message: str | None,
    ) -> None:
        job = self._get_latest_job(document=document)
        if job is None:
            return
        job.current_stage = state
        job.status = state
        job.error_message = error_message
        self.session.add(job)

    def _get_latest_job(self, *, document: Document) -> Job | None:
        return (
            self.session.execute(
                select(Job)
                .where(Job.document_id == document.id)
                .order_by(Job.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    @classmethod
    def _is_required_field_line(cls, line_text: str) -> bool:
        normalized = " ".join(line_text.lower().split())
        return any(hint in normalized for hint in cls._REQUIRED_FIELD_LINE_HINTS)

    @classmethod
    def _is_noise_low_conf_token(cls, *, token_text: str, line_text: str) -> bool:
        token = token_text.strip()
        if not token:
            return True
        if cls._PUNCTUATION_ONLY_RE.fullmatch(token):
            return True

        normalized = token.lower()
        numeric_value = normalized.replace(",", "").replace(".", "")
        is_numeric = numeric_value.isdigit() and bool(numeric_value)
        if len(normalized) <= 2 and not is_numeric:
            return True
        return normalized in cls._OCR_REVIEW_STOPWORDS and not cls._is_required_field_line(
            line_text
        )

    @staticmethod
    def _build_ocr_review_summary(
        *,
        page_number: int,
        threshold: float,
        tokens: list[Any],
        artifact_uri: str,
    ) -> dict[str, Any]:
        ranked_tokens = sorted(tokens, key=lambda token: token.confidence)
        examples = []
        for token in ranked_tokens:
            example_text = token.text.strip()
            if not example_text:
                continue
            examples.append(f"{example_text[:48]} ({token.confidence:.3f})")
            if len(examples) == 10:
                break

        confidences = [float(token.confidence) for token in ranked_tokens]
        return {
            "page_number": page_number,
            "low_conf_token_count": len(ranked_tokens),
            "examples": examples,
            "min_confidence": round(min(confidences), 3),
            "avg_confidence": round(sum(confidences) / len(confidences), 3),
            "threshold": round(threshold, 3),
            "ocr_artifact_uri": artifact_uri,
        }

    def _get_job_id(self, *, document: Document) -> str | None:
        job = self._get_latest_job(document=document)
        return str(job.id) if job is not None else None

    def _increment_job_attempt(self, *, document: Document) -> None:
        job = self._get_latest_job(document=document)
        if job is None:
            return
        job.attempt_count += 1
        self.session.add(job)

    def _log(self, *, level: str, event: str, message: str, **fields: Any) -> None:
        log_json(
            self.logger,
            level=level,
            event=event,
            message=message,
            job_id=self._job_id,
            **fields,
        )

from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..constants import MaterialSource, ReportStatus, ReviewStatus, ReviewTaskType
from ..db.models import (
    Batch,
    BatchDocument,
    Classification,
    Document,
    DocumentMaterialClassification,
    Report,
    ReviewTask,
)
from ..schemas.defra import DEFRA_REPORT_COLUMNS


@dataclass
class DocumentReportRender:
    document: Document
    rows: list[dict[str, str]]
    warnings: dict


def _row_template() -> dict[str, str]:
    return {column: "" for column in DEFRA_REPORT_COLUMNS}


def _country_value(*, document: Document, explicit: str | None) -> str:
    return (explicit or document.inferred_country_code or "").strip()


def _submission_period_warning(document: Document) -> str | None:
    if document.document_date and document.submission_period == "2025-P1":
        return (
            f"Document date extracted as {document.document_date}, "
            f"submission_period defaulted to {document.submission_period}."
        )
    return None


def _classification_to_row(document: Document, classification: Classification) -> dict[str, str]:
    row = _row_template()
    row["organisation_id"] = (
        "" if document.organisation_id is None else str(document.organisation_id)
    )
    row["subsidiary_id"] = document.subsidiary_id or ""
    row["organisation_size"] = document.organisation_size or ""
    row["submission_period"] = document.submission_period or ""
    row["packaging_activity"] = classification.packaging_activity or ""
    row["packaging_type"] = classification.packaging_type or ""
    row["packaging_class"] = classification.packaging_class or ""
    row["packaging_material"] = classification.packaging_material or ""
    row["packaging_material_subtype"] = classification.packaging_material_subtype or ""
    row["from_country"] = _country_value(document=document, explicit=classification.from_country)
    row["to_country"] = _country_value(document=document, explicit=classification.to_country)
    row["packaging_material_weight"] = (
        ""
        if classification.packaging_material_weight is None
        else str(classification.packaging_material_weight)
    )
    row["packaging_material_units"] = (
        ""
        if classification.packaging_material_units is None
        else str(classification.packaging_material_units)
    )
    row["transitional_packaging_units"] = (
        ""
        if classification.transitional_packaging_units is None
        else str(classification.transitional_packaging_units)
    )
    row["ram_rag_rating"] = classification.ram_rag_rating or ""
    return row


def _material_to_row(
    *,
    document: Document,
    material: DocumentMaterialClassification,
    base_classification: Classification | None,
) -> dict[str, str]:
    row = _row_template()
    row["organisation_id"] = (
        "" if document.organisation_id is None else str(document.organisation_id)
    )
    row["subsidiary_id"] = document.subsidiary_id or ""
    row["organisation_size"] = document.organisation_size or ""
    row["submission_period"] = document.submission_period or ""
    row["packaging_activity"] = (
        base_classification.packaging_activity if base_classification else ""
    ) or ""
    row["packaging_type"] = (
        base_classification.packaging_type if base_classification else ""
    ) or ""
    row["packaging_class"] = (
        base_classification.packaging_class if base_classification else ""
    ) or ""
    row["packaging_material"] = material.packaging_material
    row["packaging_material_subtype"] = material.packaging_material_subtype or ""
    row["from_country"] = _country_value(
        document=document,
        explicit=(base_classification.from_country if base_classification else ""),
    )
    row["to_country"] = _country_value(
        document=document,
        explicit=(base_classification.to_country if base_classification else ""),
    )
    row["packaging_material_weight"] = (
        ""
        if material.packaging_material_weight is None
        else str(material.packaging_material_weight)
    )
    # packaging_material_units is a DEFRA numeric item count column.
    # We do not currently collect item counts; weight is in packaging_material_weight.
    # The internal weight_display_unit field is NOT exported here.
    row["packaging_material_units"] = ""
    row["transitional_packaging_units"] = (
        ""
        if (base_classification is None or base_classification.transitional_packaging_units is None)
        else str(base_classification.transitional_packaging_units)
    )
    row["ram_rag_rating"] = (
        base_classification.ram_rag_rating if base_classification else ""
    ) or ""
    return row


def _build_validation_warnings(
    *,
    document: Document,
    rows: list[dict[str, str]],
    row_provenance: list[dict],
) -> dict:
    # packaging_material_units is a DEFRA numeric item count column that we do not
    # currently populate, so it is excluded from required-field checks.
    required_columns = [
        "packaging_material_weight",
        "from_country",
        "to_country",
    ]
    missing_fields_by_row: list[dict] = []
    for idx, row in enumerate(rows, start=1):
        missing_fields = [
            column for column in required_columns if not (row.get(column) or "").strip()
        ]
        if not missing_fields:
            continue
        material_key = row.get("packaging_material", "").strip()
        material_subtype = row.get("packaging_material_subtype", "").strip()
        if material_subtype:
            material_key = f"{material_key} {material_subtype}".strip()
        missing_fields_by_row.append(
            {
                "row_index": idx,
                "material_key": material_key or f"row_{idx}",
                "missing_fields": missing_fields,
            }
        )

    overall_warnings: list[str] = []
    if missing_fields_by_row:
        overall_warnings.append(
            f"{len(missing_fields_by_row)} row(s) contain missing required DEFRA fields."
        )
        missing_weight_rows = [
            row
            for row in missing_fields_by_row
            if "packaging_material_weight" in row["missing_fields"]
        ]
        if missing_weight_rows:
            overall_warnings.append(
                f"{len(missing_weight_rows)} row(s) have missing weight value."
            )

    submission_warning = _submission_period_warning(document)
    if submission_warning:
        overall_warnings.append(submission_warning)

    if document.document_type == "notice_of_liability" and any(
        (row.get("packaging_material_weight") or "").strip() for row in rows
    ):
        overall_warnings.append(
            "PackUK tonnage extracted from fee breakdown "
            "(source unit: tonnes, stored as kg)."
        )

    return {
        "missing_fields_by_row": missing_fields_by_row,
        "overall": overall_warnings,
        "document_metadata": {
            "document_type": document.document_type,
            "document_date": document.document_date,
            "submission_period": document.submission_period,
            "submission_period_warning": submission_warning,
            "country_inference": (
                {
                    "country_code": document.inferred_country_code,
                    "source": document.country_inference_source or "inferred_from_text",
                }
                if document.inferred_country_code
                else None
            ),
        },
        "row_provenance": row_provenance,
    }


def _mock_row(document: Document) -> dict[str, str]:
    row = _row_template()
    row["organisation_id"] = (
        "" if document.organisation_id is None else str(document.organisation_id)
    )
    row["subsidiary_id"] = document.subsidiary_id or ""
    row["organisation_size"] = document.organisation_size or ""
    row["submission_period"] = document.submission_period or ""
    row["from_country"] = _country_value(document=document, explicit="")
    row["to_country"] = _country_value(document=document, explicit="")
    return row


def _render_rows_for_document(session: Session, document: Document) -> DocumentReportRender:
    material_rows = (
        session.execute(
            select(DocumentMaterialClassification)
            .where(DocumentMaterialClassification.document_id == document.id)
            .order_by(DocumentMaterialClassification.created_at.asc())
        )
        .scalars()
        .all()
    )
    if material_rows:
        preferred_rows = [item for item in material_rows if item.source == MaterialSource.REVIEW]
        if not preferred_rows:
            preferred_rows = material_rows

        base_classification = (
            session.execute(
                select(Classification)
                .where(Classification.document_id == document.id)
                .order_by(Classification.created_at.desc(), Classification.row_index.asc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        rows = [
            _material_to_row(
                document=document,
                material=item,
                base_classification=base_classification,
            )
            for item in preferred_rows
        ]
        row_provenance = [
            {
                "row_index": idx,
                "document_id": str(document.id),
                "filename": document.original_filename,
                "source": item.source,
                "material_key": item.material_key,
            }
            for idx, item in enumerate(preferred_rows, start=1)
        ]
    else:
        classifications = (
            session.execute(
                select(Classification)
                .where(Classification.document_id == document.id)
                .order_by(Classification.row_index.asc())
            )
            .scalars()
            .all()
        )

        if classifications:
            rows = [_classification_to_row(document, item) for item in classifications]
            row_provenance = [
                {
                    "row_index": idx,
                    "document_id": str(document.id),
                    "filename": document.original_filename,
                    "source": item.source,
                    "material_key": (
                        f"{item.packaging_material} {item.packaging_material_subtype}".strip()
                        if item.packaging_material
                        else f"classification_row_{idx}"
                    ),
                }
                for idx, item in enumerate(classifications, start=1)
            ]
        else:
            rows = [_mock_row(document)]
            row_provenance = [
                {
                    "row_index": 1,
                    "document_id": str(document.id),
                    "filename": document.original_filename,
                    "source": "fallback_blank_row",
                    "material_key": "fallback_blank_row",
                }
            ]

    return DocumentReportRender(
        document=document,
        rows=rows,
        warnings=_build_validation_warnings(
            document=document,
            rows=rows,
            row_provenance=row_provenance,
        ),
    )


def _build_batch_validation_warnings(
    *,
    session: Session,
    renders: list[DocumentReportRender],
) -> dict:
    all_missing_fields_by_row: list[dict] = []
    per_document: list[dict] = []
    row_provenance: list[dict] = []
    cumulative_row_index = 0
    docs_missing_weights = 0
    document_ids = [render.document.id for render in renders]

    low_conf_classification_docs: set[str] = set()
    if document_ids:
        low_conf_rows = (
            session.execute(
                select(ReviewTask.document_id).where(
                    ReviewTask.document_id.in_(document_ids),
                    ReviewTask.status == ReviewStatus.PENDING,
                    ReviewTask.task_type == ReviewTaskType.CLASSIFICATION_REVIEW,
                )
            )
            .scalars()
            .all()
        )
        low_conf_classification_docs = {str(document_id) for document_id in low_conf_rows}

    for render in renders:
        missing_entries: list[dict] = []
        missing_weights_by_material: list[dict] = []
        missing_weight_count = 0
        for entry in render.warnings.get("row_provenance", []):
            row_provenance.append(
                {**entry, "row_index": cumulative_row_index + int(entry["row_index"])}
            )

        for item in render.warnings.get("missing_fields_by_row", []):
            adjusted_item = {
                **item,
                "row_index": cumulative_row_index + int(item["row_index"]),
                "document_id": str(render.document.id),
                "filename": render.document.original_filename,
            }
            all_missing_fields_by_row.append(adjusted_item)
            missing_entries.append(adjusted_item)

            if "packaging_material_weight" in item["missing_fields"]:
                missing_weight_count += 1
                missing_weights_by_material.append(
                    {
                        "material_key": item["material_key"],
                        "row_index": adjusted_item["row_index"],
                        "missing_fields": item["missing_fields"],
                    }
                )

        if missing_weight_count:
            docs_missing_weights += 1

        document_id = str(render.document.id)
        low_conf_count = 1 if document_id in low_conf_classification_docs else 0
        warning_count = len(render.warnings.get("overall", [])) + sum(
            len(item.get("missing_fields", []))
            for item in render.warnings.get("missing_fields_by_row", [])
        )
        warning_count += low_conf_count
        overall = list(render.warnings.get("overall", []))
        if low_conf_count:
            overall.append("Low-confidence classification review is pending.")

        per_document.append(
            {
                "document_id": document_id,
                "filename": render.document.original_filename,
                "document_type": render.warnings.get("document_metadata", {}).get("document_type"),
                "document_date": render.warnings.get("document_metadata", {}).get("document_date"),
                "country_inference": render.warnings.get("document_metadata", {}).get(
                    "country_inference"
                ),
                "submission_period_warning": render.warnings.get("document_metadata", {}).get(
                    "submission_period_warning"
                ),
                "warning_count": warning_count,
                "missing_weight_count": missing_weight_count,
                "missing_weights_by_material": missing_weights_by_material,
                "overall": overall,
            }
        )
        cumulative_row_index += len(render.rows)

    low_conf_doc_count = len(low_conf_classification_docs)
    overall_warnings: list[str] = []
    if all_missing_fields_by_row:
        overall_warnings.append(
            f"{len(all_missing_fields_by_row)} row(s) across {len(renders)} document(s) "
            "contain missing required DEFRA fields."
        )
    if docs_missing_weights or low_conf_doc_count:
        overall_warnings.append(
            "Batch report contains warnings: "
            f"{docs_missing_weights} doc(s) missing weights, "
            f"{low_conf_doc_count} low-confidence classifications."
        )

    total_warning_count = len(overall_warnings) + sum(
        len(item.get("missing_fields", [])) for item in all_missing_fields_by_row
    )
    total_warning_count += sum(len(render.warnings.get("overall", [])) for render in renders)
    return {
        "missing_fields_by_row": all_missing_fields_by_row,
        "overall": overall_warnings,
        "per_document": per_document,
        "row_provenance": row_provenance,
        "total_warning_count": total_warning_count,
    }


def export_report_csv(session: Session, report_id: UUID, output_dir: Path) -> tuple[Path, int]:
    csv_bytes, row_count, warnings = render_report_csv(session=session, report_id=report_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{report_id}.csv"
    output_path.write_bytes(csv_bytes)

    report = session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report {report_id} was not found")

    report.output_path = str(output_path)
    report.status = ReportStatus.GENERATED
    report.row_count = row_count
    report.validation_warnings = warnings
    session.add(report)

    return output_path, row_count


def render_report_csv(session: Session, report_id: UUID) -> tuple[bytes, int, dict]:
    report = session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report {report_id} was not found")

    if report.batch_id is not None:
        batch = session.get(Batch, report.batch_id)
        if batch is None:
            raise ValueError(f"Batch {report.batch_id} was not found")

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
            raise ValueError(f"Batch {batch.id} has no documents")

        renders = [_render_rows_for_document(session, document) for document in documents]
        rows = [row for render in renders for row in render.rows]
        warnings = _build_batch_validation_warnings(session=session, renders=renders)
    else:
        if report.document_id is None:
            raise ValueError(f"Report {report_id} is missing both document_id and batch_id")
        document = session.get(Document, report.document_id)
        if document is None:
            raise ValueError(f"Document {report.document_id} was not found")

        render = _render_rows_for_document(session, document)
        rows = render.rows
        warnings = render.warnings

    csv_buffer = StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=DEFRA_REPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return csv_buffer.getvalue().encode("utf-8"), len(rows), warnings

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Classification, Document, Report
from ..schemas.defra import DEFRA_REPORT_COLUMNS


def _row_template() -> dict[str, str]:
    return {column: "" for column in DEFRA_REPORT_COLUMNS}


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
    row["from_country"] = classification.from_country or ""
    row["to_country"] = classification.to_country or ""
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


def _mock_row(document: Document) -> dict[str, str]:
    # TODO: replace this with real extraction/classification pipeline output.
    row = _row_template()
    row["organisation_id"] = (
        "" if document.organisation_id is None else str(document.organisation_id)
    )
    row["subsidiary_id"] = document.subsidiary_id or ""
    row["organisation_size"] = document.organisation_size or "L"
    row["submission_period"] = document.submission_period or "2025-P1"
    row["packaging_activity"] = "SB"
    row["packaging_type"] = "HH"
    row["packaging_class"] = "P1"
    row["packaging_material"] = "Paper or cardboard"
    row["packaging_material_subtype"] = ""
    row["from_country"] = ""
    row["to_country"] = ""
    row["packaging_material_weight"] = "0"
    row["packaging_material_units"] = ""
    row["transitional_packaging_units"] = ""
    row["ram_rag_rating"] = ""
    return row


def export_report_csv(session: Session, report_id: UUID, output_dir: Path) -> tuple[Path, int]:
    csv_bytes, row_count = render_report_csv(session=session, report_id=report_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{report_id}.csv"
    output_path.write_bytes(csv_bytes)

    report = session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report {report_id} was not found")

    report.output_path = str(output_path)
    report.status = "generated"
    report.row_count = row_count
    session.add(report)

    return output_path, row_count


def render_report_csv(session: Session, report_id: UUID) -> tuple[bytes, int]:
    report = session.get(Report, report_id)
    if report is None:
        raise ValueError(f"Report {report_id} was not found")

    document = session.get(Document, report.document_id)
    if document is None:
        raise ValueError(f"Document {report.document_id} was not found")

    classifications = (
        session.execute(
            select(Classification)
            .where(Classification.document_id == report.document_id)
            .order_by(Classification.row_index.asc())
        )
        .scalars()
        .all()
    )

    if classifications:
        rows = [_classification_to_row(document, item) for item in classifications]
    else:
        rows = [_mock_row(document)]

    csv_buffer = StringIO()
    writer = csv.DictWriter(csv_buffer, fieldnames=DEFRA_REPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return csv_buffer.getvalue().encode("utf-8"), len(rows)

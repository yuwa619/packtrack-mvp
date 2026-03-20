from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, select

from ..config import settings
from ..db.models import Batch, BatchDocument, Document, Report
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context
from ..schemas.defra import DEFRA_REPORT_COLUMNS
from ..services.idempotency import IdempotencyGuard
from ..services.report_export import render_report_csv
from ..services.storage import ObjectStorage

router = APIRouter(prefix="/reports", tags=["reports"])


def _warning_count(validation_warnings: dict | None) -> int:
    if not validation_warnings:
        return 0
    total_warning_count = validation_warnings.get("total_warning_count")
    if isinstance(total_warning_count, int):
        return total_warning_count
    missing_fields_by_row = validation_warnings.get("missing_fields_by_row", [])
    overall = validation_warnings.get("overall", [])
    missing_field_entries = sum(
        len(item.get("missing_fields", []))
        for item in missing_fields_by_row
        if isinstance(item, dict)
    )
    return len(overall) + missing_field_entries


def _build_report_filename(batch: Batch, document_count: int) -> str:
    base_name = (batch.name or "").strip() or f"Batch {str(batch.id)[:8]}"
    return f"{base_name} ({document_count} docs)"


def _serialise_report(
    *,
    report: Report,
    filename: str,
    document_id: UUID | None,
    batch_id: UUID | None,
    document_count: int | None,
) -> dict[str, object]:
    return {
        "report_id": str(report.id),
        "document_id": str(document_id) if document_id is not None else None,
        "batch_id": str(batch_id) if batch_id is not None else None,
        "filename": filename,
        "status": report.status,
        "row_count": report.row_count,
        "warning_count": _warning_count(report.validation_warnings),
        "validation_warnings": report.validation_warnings,
        "submission_period": report.submission_period,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "download_endpoint": f"/api/v1/reports/{report.id}/download",
        "report_scope": "batch" if batch_id is not None else "document",
        "document_count": document_count,
    }


def _resolve_authorised_report(
    *,
    session,
    report_id: UUID,
    tenant_id: int,
) -> tuple[Report, dict[str, object]]:
    report = session.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} was not found")

    if report.batch_id is not None:
        batch = session.get(Batch, report.batch_id)
        if batch is None or batch.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail=f"Report {report_id} was not found")
        document_count = int(
            session.execute(
                select(func.count())
                .select_from(BatchDocument)
                .where(BatchDocument.batch_id == batch.id)
            ).scalar_one()
        )
        return report, _serialise_report(
            report=report,
            filename=_build_report_filename(batch, document_count),
            document_id=None,
            batch_id=batch.id,
            document_count=document_count,
        )

    if report.document_id is None:
        raise HTTPException(status_code=404, detail=f"Report {report_id} was not found")
    document = session.get(Document, report.document_id)
    if document is None or document.organisation_id != tenant_id:
        raise HTTPException(status_code=404, detail=f"Report {report_id} was not found")
    return report, _serialise_report(
        report=report,
        filename=document.original_filename,
        document_id=document.id,
        batch_id=None,
        document_count=1,
    )


@router.get("/schema")
def get_report_schema() -> dict[str, list[str]]:
    return {"columns": DEFRA_REPORT_COLUMNS}


@router.get("")
def list_reports(auth: Annotated[AuthContext, Depends(get_auth_context)]) -> dict[str, list[dict]]:
    with db_session() as session:
        reports = session.execute(select(Report).order_by(Report.created_at.desc())).scalars().all()
        payload: list[dict] = []
        for report in reports:
            try:
                _report, serialized = _resolve_authorised_report(
                    session=session,
                    report_id=report.id,
                    tenant_id=auth.tenant_id,
                )
            except HTTPException:
                continue
            payload.append(serialized)

    return {"reports": payload}


@router.get("/{report_id}")
def get_report_detail(
    report_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict:
    with db_session() as session:
        _report, payload = _resolve_authorised_report(
            session=session,
            report_id=report_id,
            tenant_id=auth.tenant_id,
        )
        return payload


@router.post("/{report_id}/export")
def export_report(
    report_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    storage = ObjectStorage()
    with db_session() as session:
        report, payload = _resolve_authorised_report(
            session=session,
            report_id=report_id,
            tenant_id=auth.tenant_id,
        )

        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="report_export",
            idempotency_key=idempotency_key,
            request_payload={"report_id": str(report_id)},
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        try:
            csv_bytes, row_count, warnings = render_report_csv(
                session=session,
                report_id=report_id,
            )
        except ValueError as exc:
            idempotency.failure(status_code=404, detail=str(exc))
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            idempotency.failure(status_code=500, detail=str(exc))
            raise

        report.output_path = storage.put_bytes(
            bucket=settings.minio_bucket_reports,
            key=f"reports/{report.id}.csv",
            data=csv_bytes,
            content_type="text/csv",
        )
        report.status = "generated"
        report.row_count = row_count
        report.validation_warnings = warnings
        session.add(report)

        response = {
            **payload,
            "row_count": row_count,
            "warning_count": _warning_count(warnings),
            "validation_warnings": warnings,
            "csv_path": report.output_path,
            "schema_version": "defra-v1",
        }
        idempotency.success(response)
    return response


@router.get("/{report_id}/download")
def download_report(
    report_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
) -> Response:
    with db_session() as session:
        report, payload = _resolve_authorised_report(
            session=session,
            report_id=report_id,
            tenant_id=auth.tenant_id,
        )
        if not report.output_path:
            raise HTTPException(status_code=409, detail="Report file is not generated yet")

        bucket, key = ObjectStorage.parse_uri(report.output_path)
        csv_bytes = ObjectStorage().get_bytes(bucket=bucket, key=key)

        if payload["report_scope"] == "batch":
            base_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(payload["filename"])).strip("_")
        else:
            base_name = str(payload["document_id"])
        filename = f"{base_name}_{report.submission_period or 'submission'}_packaging_data.csv"

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

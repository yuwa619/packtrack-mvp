from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from ..db.models import Document, Report
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context
from ..schemas.defra import DEFRA_REPORT_COLUMNS
from ..services.idempotency import IdempotencyGuard
from ..services.report_export import export_report_csv
from ..services.storage import ObjectStorage

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/schema")
def get_report_schema() -> dict[str, list[str]]:
    return {"columns": DEFRA_REPORT_COLUMNS}


@router.get("")
def list_reports(auth: Annotated[AuthContext, Depends(get_auth_context)]) -> dict[str, list[dict]]:
    with db_session() as session:
        rows = session.execute(
            select(Report, Document)
            .join(Document, Report.document_id == Document.id)
            .where(Document.organisation_id == auth.tenant_id)
            .order_by(Report.created_at.desc())
        ).all()
        payload = [
            {
                "report_id": str(report.id),
                "document_id": str(report.document_id),
                "filename": document.original_filename,
                "status": report.status,
                "row_count": report.row_count,
                "submission_period": report.submission_period,
                "created_at": report.created_at.isoformat() if report.created_at else None,
                "download_endpoint": f"/api/v1/reports/{report.id}/download",
            }
            for report, document in rows
        ]

    return {"reports": payload}


@router.post("/{report_id}/export")
def export_report(
    report_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, str | int]:
    with db_session() as session:
        row = session.execute(
            select(Report, Document)
            .join(Document, Report.document_id == Document.id)
            .where(Report.id == report_id, Document.organisation_id == auth.tenant_id)
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Report {report_id} was not found")

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
            output_path, row_count = export_report_csv(
                session=session,
                report_id=report_id,
                output_dir=Path("data/exports"),
            )
        except ValueError as exc:
            idempotency.failure(status_code=404, detail=str(exc))
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            idempotency.failure(status_code=500, detail=str(exc))
            raise

        response = {
            "report_id": str(report_id),
            "row_count": row_count,
            "csv_path": str(output_path),
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
        row = session.execute(
            select(Report, Document)
            .join(Document, Report.document_id == Document.id)
            .where(Report.id == report_id, Document.organisation_id == auth.tenant_id)
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"Report {report_id} was not found")
        report, document = row
        if not report.output_path:
            raise HTTPException(status_code=409, detail="Report file is not generated yet")

        bucket, key = ObjectStorage.parse_uri(report.output_path)
        csv_bytes = ObjectStorage().get_bytes(bucket=bucket, key=key)
        filename = f"{document.id}_{report.submission_period or 'submission'}_packaging_data.csv"

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..db.models import Document, Job, Report
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
def list_jobs(auth: Annotated[AuthContext, Depends(get_auth_context)]) -> dict[str, list[dict]]:
    with db_session() as session:
        jobs = session.execute(
            select(Job, Document)
            .join(Document, Job.document_id == Document.id)
            .where(Document.organisation_id == auth.tenant_id)
            .order_by(Job.created_at.desc())
        ).all()

        report_rows = session.execute(
            select(Report.document_id, Report.id, Report.status)
            .join(Document, Report.document_id == Document.id)
            .where(Document.organisation_id == auth.tenant_id)
            .order_by(Report.created_at.desc())
        ).all()
        report_map: dict[str, dict] = {}
        for document_id, report_id, status in report_rows:
            key = str(document_id)
            if key not in report_map:
                report_map[key] = {
                    "report_id": str(report_id),
                    "status": status,
                }

        payload = []
        for job, document in jobs:
            payload.append(
                {
                    "job_id": str(job.id),
                    "document_id": str(document.id),
                    "filename": document.original_filename,
                    "status": document.status,
                    "current_stage": job.current_stage,
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                    "report": report_map.get(str(document.id)),
                }
            )

    return {"jobs": payload}

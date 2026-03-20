from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..db.models import Document, ExtractedEntity, Job, Report, ReviewTask
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_admin_auth_context

router = APIRouter(prefix="/metrics", tags=["metrics"])


def _duration_expr_for_dialect(dialect_name: str):
    if dialect_name == "sqlite":
        return (func.julianday(Job.updated_at) - func.julianday(Job.created_at)) * 86400.0
    return func.extract("epoch", Job.updated_at - Job.created_at)


@router.get("/summary")
def get_metrics_summary(
    _admin: Annotated[AuthContext, Depends(get_admin_auth_context)],
) -> dict[str, object]:
    cutoff = datetime.utcnow() - timedelta(hours=24)

    with db_session() as session:
        processed_docs_query = select(Document.id).where(
            Document.updated_at >= cutoff,
            Document.status.in_(["COMPLETE", "FAILED"]),
        )
        processed_docs_subquery = processed_docs_query.subquery()

        docs_processed_24h = (
            session.execute(select(func.count()).select_from(processed_docs_subquery)).scalar() or 0
        )

        duration_expr = _duration_expr_for_dialect(session.bind.dialect.name)
        avg_pipeline_time_sec = (
            session.execute(
                select(func.avg(duration_expr)).where(
                    Job.updated_at >= cutoff,
                    Job.status == "COMPLETE",
                )
            ).scalar()
            or 0.0
        )

        docs_with_review_tasks = (
            session.execute(
                select(func.count(func.distinct(ReviewTask.document_id))).where(
                    ReviewTask.document_id.in_(select(processed_docs_subquery.c.id))
                )
            ).scalar()
            or 0
        )
        pct_docs_with_review_tasks = (
            float(docs_with_review_tasks) / float(docs_processed_24h) * 100.0
            if docs_processed_24h
            else 0.0
        )

        failure_rows = session.execute(
            select(
                Job.error_message.label("reason"),
                func.count(Job.id).label("count"),
            )
            .where(
                Job.status == "FAILED",
                Job.updated_at >= cutoff,
                Job.error_message.is_not(None),
            )
            .group_by(Job.error_message)
            .order_by(func.count(Job.id).desc(), Job.error_message.asc())
            .limit(5)
        ).all()

        supplier_value = func.coalesce(
            func.nullif(ExtractedEntity.normalized_value, ""),
            func.nullif(ExtractedEntity.raw_value, ""),
        )
        supplier_rows = session.execute(
            select(
                supplier_value.label("supplier"),
                func.count(ExtractedEntity.id).label("count"),
            )
            .where(
                ExtractedEntity.field_name.in_(["supplier_name", "supplier_ref"]),
                supplier_value.is_not(None),
            )
            .group_by(supplier_value)
            .order_by(func.count(ExtractedEntity.id).desc(), supplier_value.asc())
            .limit(5)
        ).all()

        reports_generated_24h = (
            session.execute(
                select(func.count(Report.id)).where(
                    Report.created_at >= cutoff,
                    Report.status == "generated",
                )
            ).scalar()
            or 0
        )

    return {
        "docs_processed_24h": int(docs_processed_24h),
        "avg_pipeline_time_sec": round(float(avg_pipeline_time_sec), 2),
        "pct_docs_with_review_tasks": round(float(pct_docs_with_review_tasks), 2),
        "top_5_failure_reasons": [
            {"reason": reason, "count": int(count)} for reason, count in failure_rows
        ],
        "top_5_suppliers": [
            {"supplier": supplier, "count": int(count)}
            for supplier, count in supplier_rows
            if supplier
        ],
        "reports_generated_24h": int(reports_generated_24h),
    }

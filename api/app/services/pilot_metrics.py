from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Document, ExtractedEntity, Job, Report, ReviewTask

_REQUIRED_FIELDS = ("invoice_ref", "invoice_date", "product_desc", "weight_value", "weight_unit")
_TOTAL_REQUIRED_FIELDS = len(_REQUIRED_FIELDS) + 1  # + supplier_ref_or_name


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[94]


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return float(numerator) / float(denominator) * 100.0


def _doc_ids_processed_last_7d(*, session: Session, cutoff: datetime) -> list[Any]:
    rows = session.execute(
        select(Document.id).where(
            Document.updated_at >= cutoff,
            Document.status.in_(["COMPLETE", "FAILED"]),
        )
    ).all()
    return [row[0] for row in rows]


def _runtime_stats_last_7d(*, session: Session, cutoff: datetime) -> tuple[float, float]:
    rows = session.execute(
        select(Job.created_at, Job.updated_at).where(
            Job.updated_at >= cutoff,
            Job.status == "COMPLETE",
        )
    ).all()
    durations: list[float] = []
    for created_at, updated_at in rows:
        if created_at is None or updated_at is None:
            continue
        duration_sec = max(0.0, (updated_at - created_at).total_seconds())
        durations.append(duration_sec)
    if not durations:
        return 0.0, 0.0
    return statistics.mean(durations), _p95(durations)


def _review_docs_set(*, session: Session, doc_ids: list[Any]) -> set[Any]:
    if not doc_ids:
        return set()
    rows = session.execute(
        select(ReviewTask.document_id)
        .where(ReviewTask.document_id.in_(doc_ids))
        .distinct()
    ).all()
    return {row[0] for row in rows if row[0] is not None}


def _extraction_coverage(*, session: Session, doc_ids: list[Any]) -> float:
    if not doc_ids:
        return 0.0
    rows = session.execute(
        select(ExtractedEntity.document_id, ExtractedEntity.field_name).where(
            ExtractedEntity.document_id.in_(doc_ids)
        )
    ).all()

    per_doc_fields: dict[Any, set[str]] = {doc_id: set() for doc_id in doc_ids}
    for document_id, field_name in rows:
        if document_id is None or not field_name:
            continue
        per_doc_fields.setdefault(document_id, set()).add(str(field_name))

    present_required_total = 0
    for doc_id in doc_ids:
        fields = per_doc_fields.get(doc_id, set())
        present_required = sum(1 for field in _REQUIRED_FIELDS if field in fields)
        if "supplier_ref" in fields or "supplier_name" in fields:
            present_required += 1
        present_required_total += present_required

    max_required_total = len(doc_ids) * _TOTAL_REQUIRED_FIELDS
    return _safe_rate(present_required_total, max_required_total)


def _top_failure_reasons(*, session: Session, cutoff: datetime) -> list[dict[str, Any]]:
    rows = session.execute(
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
    return [{"reason": reason, "count": int(count)} for reason, count in rows]


def _top_suppliers_by_review_rate(
    *,
    session: Session,
    doc_ids: list[Any],
    review_docs: set[Any],
) -> list[dict[str, Any]]:
    if not doc_ids:
        return []

    supplier_rows = session.execute(
        select(
            ExtractedEntity.document_id,
            func.coalesce(
                func.nullif(ExtractedEntity.normalized_value, ""),
                func.nullif(ExtractedEntity.raw_value, ""),
            ).label("supplier"),
        )
        .where(
            ExtractedEntity.document_id.in_(doc_ids),
            ExtractedEntity.field_name.in_(["supplier_name", "supplier_ref"]),
        )
        .order_by(ExtractedEntity.document_id.asc())
    ).all()

    doc_supplier: dict[Any, str] = {}
    for document_id, supplier in supplier_rows:
        if document_id is None or not supplier:
            continue
        doc_supplier.setdefault(document_id, str(supplier))

    grouped: dict[str, dict[str, int]] = {}
    for doc_id, supplier in doc_supplier.items():
        bucket = grouped.setdefault(supplier, {"doc_count": 0, "review_doc_count": 0})
        bucket["doc_count"] += 1
        if doc_id in review_docs:
            bucket["review_doc_count"] += 1

    ranked = sorted(
        (
            {
                "supplier": supplier,
                "doc_count": values["doc_count"],
                "review_doc_count": values["review_doc_count"],
                "review_rate_pct": round(
                    _safe_rate(values["review_doc_count"], values["doc_count"]), 2
                ),
            }
            for supplier, values in grouped.items()
        ),
        key=lambda item: (
            -item["review_rate_pct"],
            -item["review_doc_count"],
            -item["doc_count"],
            item["supplier"],
        ),
    )
    return ranked[:5]


def _top_templates_by_review_rate(
    *,
    session: Session,
    doc_ids: list[Any],
    review_docs: set[Any],
) -> list[dict[str, Any]]:
    if not doc_ids:
        return []

    rows = session.execute(
        select(Document.id, Document.original_filename).where(Document.id.in_(doc_ids))
    ).all()
    grouped: dict[str, dict[str, int]] = {}
    for document_id, original_filename in rows:
        if document_id is None:
            continue
        template = (original_filename or "").strip() or "unknown"
        doc_id = document_id
        bucket = grouped.setdefault(template, {"doc_count": 0, "review_doc_count": 0})
        bucket["doc_count"] += 1
        if doc_id in review_docs:
            bucket["review_doc_count"] += 1

    ranked = sorted(
        (
            {
                "template": template,
                "doc_count": values["doc_count"],
                "review_doc_count": values["review_doc_count"],
                "review_rate_pct": round(
                    _safe_rate(values["review_doc_count"], values["doc_count"]), 2
                ),
            }
            for template, values in grouped.items()
        ),
        key=lambda item: (
            -item["review_rate_pct"],
            -item["review_doc_count"],
            -item["doc_count"],
            item["template"],
        ),
    )
    return ranked[:5]


def get_pilot_summary(*, session: Session, window_days: int = 7) -> dict[str, Any]:
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    doc_ids = _doc_ids_processed_last_7d(session=session, cutoff=cutoff)
    docs_processed_7d = len(doc_ids)

    reports_generated_7d = (
        session.execute(
            select(func.count(Report.id)).where(
                Report.created_at >= cutoff,
                Report.status == "generated",
            )
        ).scalar()
        or 0
    )

    avg_runtime, p95_runtime = _runtime_stats_last_7d(session=session, cutoff=cutoff)
    review_docs = _review_docs_set(session=session, doc_ids=doc_ids)
    review_task_rate_pct = _safe_rate(len(review_docs), docs_processed_7d)
    extraction_coverage_pct = _extraction_coverage(session=session, doc_ids=doc_ids)

    return {
        "docs_processed_7d": int(docs_processed_7d),
        "reports_generated_7d": int(reports_generated_7d),
        "avg_pipeline_runtime_sec": round(float(avg_runtime), 2),
        "p95_pipeline_runtime_sec": round(float(p95_runtime), 2),
        "review_task_rate_pct": round(float(review_task_rate_pct), 2),
        "extraction_coverage_pct": round(float(extraction_coverage_pct), 2),
        "top_failure_reasons": _top_failure_reasons(session=session, cutoff=cutoff),
        "top_suppliers_by_review_rate": _top_suppliers_by_review_rate(
            session=session,
            doc_ids=doc_ids,
            review_docs=review_docs,
        ),
        "top_templates_by_review_rate": _top_templates_by_review_rate(
            session=session,
            doc_ids=doc_ids,
            review_docs=review_docs,
        ),
    }

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import api.app.db.session as db_session_module
from api.app.db.base import Base
from api.app.db.models import Document, ExtractedEntity, Job, Report, ReviewTask
from api.app.main import app


def _setup_sqlite(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'admin-pilot-metrics.db'}",
        connect_args={"check_same_thread": False},
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    return testing_session_local


def _extracted(
    *,
    document_id,
    field_name: str,
    value: str,
) -> ExtractedEntity:
    return ExtractedEntity(
        id=uuid4(),
        document_id=document_id,
        page_id=None,
        field_name=field_name,
        raw_value=value,
        normalized_value=value,
        confidence=1.0,
        source_page_number=1,
        source_block_number=None,
        source_line_number=None,
        start_offset=None,
        end_offset=None,
        provenance={},
    )


def test_admin_pilot_metrics_requires_admin_role(tmp_path, monkeypatch) -> None:
    _setup_sqlite(tmp_path, monkeypatch)
    client = TestClient(app)

    unauth = client.get("/api/v1/admin/metrics/pilot-summary")
    assert unauth.status_code == 401

    non_admin = client.get(
        "/api/v1/admin/metrics/pilot-summary",
        headers={"X-User-Id": "alice", "X-Tenant-Id": "123456"},
    )
    assert non_admin.status_code == 403

    admin = client.get(
        "/api/v1/admin/metrics/pilot-summary",
        headers={
            "X-User-Id": "ops-admin",
            "X-Tenant-Id": "123456",
            "X-User-Role": "admin",
        },
    )
    assert admin.status_code == 200


def test_admin_pilot_metrics_returns_expected_summary(tmp_path, monkeypatch) -> None:
    testing_session_local = _setup_sqlite(tmp_path, monkeypatch)
    now = datetime.utcnow()
    old = now - timedelta(days=10)

    doc_1 = Document(
        id=uuid4(),
        organisation_id=123456,
        original_filename="template-a.pdf",
        mime_type="application/pdf",
        storage_path="minio://raw-uploads/doc1.pdf",
        status="COMPLETE",
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=1),
    )
    doc_2 = Document(
        id=uuid4(),
        organisation_id=123456,
        original_filename="template-b.pdf",
        mime_type="application/pdf",
        storage_path="minio://raw-uploads/doc2.pdf",
        status="FAILED",
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(minutes=30),
    )
    doc_old = Document(
        id=uuid4(),
        organisation_id=123456,
        original_filename="template-old.pdf",
        mime_type="application/pdf",
        storage_path="minio://raw-uploads/doc-old.pdf",
        status="COMPLETE",
        created_at=old - timedelta(hours=1),
        updated_at=old,
    )

    with testing_session_local() as session:
        session.add_all([doc_1, doc_2, doc_old])
        session.flush()

        session.add_all(
            [
                Job(
                    id=uuid4(),
                    document_id=doc_1.id,
                    organisation_id=123456,
                    status="COMPLETE",
                    current_stage="COMPLETE",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=1,
                    error_message=None,
                    created_at=now - timedelta(minutes=20),
                    updated_at=now - timedelta(minutes=18),
                ),
                Job(
                    id=uuid4(),
                    document_id=doc_2.id,
                    organisation_id=123456,
                    status="COMPLETE",
                    current_stage="COMPLETE",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=1,
                    error_message=None,
                    created_at=now - timedelta(minutes=40),
                    updated_at=now - timedelta(minutes=35),
                ),
                Job(
                    id=uuid4(),
                    document_id=doc_2.id,
                    organisation_id=123456,
                    status="FAILED",
                    current_stage="FAILED",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=3,
                    error_message="ocr timeout",
                    created_at=now - timedelta(minutes=15),
                    updated_at=now - timedelta(minutes=10),
                ),
                Job(
                    id=uuid4(),
                    document_id=doc_old.id,
                    organisation_id=123456,
                    status="FAILED",
                    current_stage="FAILED",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=3,
                    error_message="old failure",
                    created_at=old - timedelta(minutes=30),
                    updated_at=old - timedelta(minutes=25),
                ),
            ]
        )

        session.add_all(
            [
                Report(
                    id=uuid4(),
                    document_id=doc_1.id,
                    submission_period="2025-P1",
                    output_path="minio://reports/doc1.csv",
                    status="generated",
                    row_count=1,
                    created_at=now - timedelta(minutes=15),
                ),
                Report(
                    id=uuid4(),
                    document_id=doc_2.id,
                    submission_period="2025-P1",
                    output_path="minio://reports/doc2.csv",
                    status="generated",
                    row_count=1,
                    created_at=now - timedelta(minutes=12),
                ),
                Report(
                    id=uuid4(),
                    document_id=doc_old.id,
                    submission_period="2025-P1",
                    output_path="minio://reports/doc-old.csv",
                    status="generated",
                    row_count=1,
                    created_at=old,
                ),
            ]
        )

        session.add(
            ReviewTask(
                id=uuid4(),
                document_id=doc_1.id,
                classification_id=None,
                task_type="EXTRACTION_REVIEW",
                status="pending",
                notes="missing field",
            )
        )

        session.add_all(
            [
                _extracted(document_id=doc_1.id, field_name="invoice_ref", value="INV-1"),
                _extracted(document_id=doc_1.id, field_name="invoice_date", value="2026-03-01"),
                _extracted(document_id=doc_1.id, field_name="product_desc", value="PET bottle"),
                _extracted(document_id=doc_1.id, field_name="weight_value", value="1.0"),
                _extracted(document_id=doc_1.id, field_name="weight_unit", value="kg"),
                _extracted(document_id=doc_1.id, field_name="supplier_name", value="Acme Ltd"),
                _extracted(document_id=doc_2.id, field_name="invoice_ref", value="INV-2"),
                _extracted(document_id=doc_2.id, field_name="invoice_date", value="2026-03-02"),
                _extracted(document_id=doc_2.id, field_name="supplier_name", value="Bravo Plc"),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get(
        "/api/v1/admin/metrics/pilot-summary",
        headers={
            "X-User-Id": "ops-admin",
            "X-Tenant-Id": "123456",
            "X-User-Role": "admin",
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["docs_processed_7d"] == 2
    assert payload["reports_generated_7d"] == 2
    assert payload["avg_pipeline_runtime_sec"] == 210.0
    assert payload["p95_pipeline_runtime_sec"] == 291.0
    assert payload["review_task_rate_pct"] == 50.0
    assert payload["extraction_coverage_pct"] == 75.0
    assert payload["top_failure_reasons"] == [{"reason": "ocr timeout", "count": 1}]

    assert payload["top_suppliers_by_review_rate"][0]["supplier"] == "Acme Ltd"
    assert payload["top_suppliers_by_review_rate"][0]["review_rate_pct"] == 100.0
    assert payload["top_templates_by_review_rate"][0]["template"] == "template-a.pdf"
    assert payload["top_templates_by_review_rate"][0]["review_rate_pct"] == 100.0

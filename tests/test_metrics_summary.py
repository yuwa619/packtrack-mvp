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
        f"sqlite+pysqlite:///{tmp_path / 'metrics.db'}",
        connect_args={"check_same_thread": False},
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    return testing_session_local


def test_metrics_summary_requires_admin_role(tmp_path, monkeypatch) -> None:
    _setup_sqlite(tmp_path, monkeypatch)
    client = TestClient(app)

    unauthenticated = client.get("/api/v1/metrics/summary")
    assert unauthenticated.status_code == 401

    non_admin = client.get(
        "/api/v1/metrics/summary",
        headers={"X-User-Id": "alice", "X-Tenant-Id": "123456"},
    )
    assert non_admin.status_code == 403

    admin = client.get(
        "/api/v1/metrics/summary",
        headers={
            "X-User-Id": "ops-admin",
            "X-Tenant-Id": "123456",
            "X-User-Role": "admin",
        },
    )
    assert admin.status_code == 200
    payload = admin.json()
    assert set(payload.keys()) == {
        "docs_processed_24h",
        "avg_pipeline_time_sec",
        "pct_docs_with_review_tasks",
        "top_5_failure_reasons",
        "top_5_suppliers",
        "reports_generated_24h",
    }


def test_metrics_summary_returns_expected_aggregates(tmp_path, monkeypatch) -> None:
    testing_session_local = _setup_sqlite(tmp_path, monkeypatch)
    now = datetime.utcnow()
    old = now - timedelta(days=2)

    doc_complete_recent = Document(
        id=uuid4(),
        organisation_id=123456,
        original_filename="recent-complete.pdf",
        mime_type="application/pdf",
        storage_path="minio://raw-uploads/d1.pdf",
        status="COMPLETE",
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
    )
    doc_failed_recent = Document(
        id=uuid4(),
        organisation_id=123456,
        original_filename="recent-failed.pdf",
        mime_type="application/pdf",
        storage_path="minio://raw-uploads/d2.pdf",
        status="FAILED",
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(minutes=30),
    )
    doc_complete_old = Document(
        id=uuid4(),
        organisation_id=123456,
        original_filename="old-complete.pdf",
        mime_type="application/pdf",
        storage_path="minio://raw-uploads/d3.pdf",
        status="COMPLETE",
        created_at=old - timedelta(hours=2),
        updated_at=old - timedelta(hours=1),
    )

    with testing_session_local() as session:
        session.add_all([doc_complete_recent, doc_failed_recent, doc_complete_old])
        session.flush()

        session.add_all(
            [
                Job(
                    id=uuid4(),
                    document_id=doc_complete_recent.id,
                    organisation_id=123456,
                    status="COMPLETE",
                    current_stage="COMPLETE",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=0,
                    error_message=None,
                    created_at=now - timedelta(minutes=10),
                    updated_at=now - timedelta(minutes=8),
                ),
                Job(
                    id=uuid4(),
                    document_id=doc_failed_recent.id,
                    organisation_id=123456,
                    status="FAILED",
                    current_stage="FAILED",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=3,
                    error_message="ocr timeout",
                    created_at=now - timedelta(minutes=15),
                    updated_at=now - timedelta(minutes=5),
                ),
                Job(
                    id=uuid4(),
                    document_id=doc_failed_recent.id,
                    organisation_id=123456,
                    status="FAILED",
                    current_stage="FAILED",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=3,
                    error_message="ocr timeout",
                    created_at=now - timedelta(minutes=20),
                    updated_at=now - timedelta(minutes=4),
                ),
                Job(
                    id=uuid4(),
                    document_id=doc_complete_old.id,
                    organisation_id=123456,
                    status="COMPLETE",
                    current_stage="COMPLETE",
                    queue_name="packtrack:queue:preprocess",
                    attempt_count=0,
                    error_message=None,
                    created_at=old - timedelta(minutes=5),
                    updated_at=old - timedelta(minutes=2),
                ),
            ]
        )

        session.add(
            ReviewTask(
                id=uuid4(),
                document_id=doc_complete_recent.id,
                classification_id=None,
                task_type="OCR_REVIEW",
                status="pending",
                notes="low confidence",
            )
        )

        session.add_all(
            [
                ExtractedEntity(
                    id=uuid4(),
                    document_id=doc_complete_recent.id,
                    page_id=None,
                    field_name="supplier_name",
                    raw_value="Acme Ltd",
                    normalized_value="Acme Ltd",
                    confidence=1.0,
                    source_page_number=1,
                    source_block_number=None,
                    source_line_number=None,
                    start_offset=None,
                    end_offset=None,
                    provenance={},
                ),
                ExtractedEntity(
                    id=uuid4(),
                    document_id=doc_failed_recent.id,
                    page_id=None,
                    field_name="supplier_name",
                    raw_value="Acme Ltd",
                    normalized_value="Acme Ltd",
                    confidence=1.0,
                    source_page_number=1,
                    source_block_number=None,
                    source_line_number=None,
                    start_offset=None,
                    end_offset=None,
                    provenance={},
                ),
                ExtractedEntity(
                    id=uuid4(),
                    document_id=doc_complete_old.id,
                    page_id=None,
                    field_name="supplier_name",
                    raw_value="Legacy Supplier",
                    normalized_value="Legacy Supplier",
                    confidence=1.0,
                    source_page_number=1,
                    source_block_number=None,
                    source_line_number=None,
                    start_offset=None,
                    end_offset=None,
                    provenance={},
                ),
            ]
        )

        session.add_all(
            [
                Report(
                    id=uuid4(),
                    document_id=doc_complete_recent.id,
                    submission_period="2025-P1",
                    output_path="minio://reports/recent.csv",
                    status="generated",
                    row_count=1,
                    created_at=now - timedelta(minutes=2),
                ),
                Report(
                    id=uuid4(),
                    document_id=doc_complete_old.id,
                    submission_period="2025-P1",
                    output_path="minio://reports/old.csv",
                    status="generated",
                    row_count=1,
                    created_at=old,
                ),
            ]
        )

        session.commit()

    client = TestClient(app)
    response = client.get(
        "/api/v1/metrics/summary",
        headers={
            "X-User-Id": "ops-admin",
            "X-Tenant-Id": "123456",
            "X-User-Role": "admin",
        },
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["docs_processed_24h"] == 2
    assert payload["avg_pipeline_time_sec"] == 120.0
    assert payload["pct_docs_with_review_tasks"] == 50.0
    assert payload["reports_generated_24h"] == 1

    assert payload["top_5_failure_reasons"] == [{"reason": "ocr timeout", "count": 2}]
    assert payload["top_5_suppliers"][0] == {"supplier": "Acme Ltd", "count": 2}

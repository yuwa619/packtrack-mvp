from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import api.app.db.session as db_session_module
from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import (
    Classification,
    Document,
    IdempotencyRecord,
    Job,
    Report,
    TaxonomyCode,
)
from api.app.main import app
from api.app.routers import documents as documents_router
from api.app.services import ocr as ocr_service
from api.app.services import preprocess as preprocess_service
from api.app.services.storage import ObjectStorage


def _seed_taxonomy(session) -> None:
    entries = [
        ("Packaging Activity", "SB", "Supplied under your brand"),
        ("Packaging Activity", "IM", "Imported"),
        ("Packaging Type", "HH", "Household packaging"),
        ("Packaging Type", "NH", "Non-household packaging"),
        ("Packaging Class", "P1", "Primary packaging"),
        ("Packaging Class", "P2", "Secondary packaging"),
        ("Material", "Plastic", "Plastic"),
        ("Material", "Paper or cardboard", "Paper or cardboard"),
        ("Material", "Glass", "Glass"),
        ("Material", "Wood", "Wood"),
        ("Plastic Sub-type", "Rigid", "Rigid plastic"),
        ("Plastic Sub-type", "Flexible", "Flexible plastic"),
    ]
    for idx, (category, code, description) in enumerate(entries, start=1):
        session.add(
            TaxonomyCode(
                category=category,
                code=code,
                description=description,
                source_sheet="taxonomy for the UK DEFRA Exten",
                source_row_number=idx,
                active=True,
            )
        )
    session.commit()


def _setup_sqlite(tmp_path, monkeypatch):
    sqlite_path = tmp_path / "hardening.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)
    return testing_session_local


def test_upload_finalise_idempotency_replays_without_duplicate_enqueue(
    tmp_path, monkeypatch
) -> None:
    testing_session_local = _setup_sqlite(tmp_path, monkeypatch)

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class FakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", FakeJobQueue)

    client = TestClient(app)
    auth_headers = {"X-User-Id": "alice", "X-Tenant-Id": "123456"}
    file_bytes = b"%PDF-1.4\n%idempotency"

    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers=auth_headers,
        json={
            "filename": "idempotency.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(file_bytes),
        },
    )
    assert presign_response.status_code == 200
    presigned = presign_response.json()

    ObjectStorage().put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=file_bytes,
        content_type="application/pdf",
    )

    idempotency_headers = {**auth_headers, "Idempotency-Key": "upload-finalise-1"}
    first = client.post(
        "/api/v1/documents/upload/finalise",
        headers=idempotency_headers,
        json={"upload_id": presigned["upload_id"]},
    )
    second = client.post(
        "/api/v1/documents/upload/finalise",
        headers=idempotency_headers,
        json={"upload_id": presigned["upload_id"]},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert len(enqueued) == 1

    with testing_session_local() as session:
        documents = session.execute(select(Document)).scalars().all()
        jobs = session.execute(select(Job)).scalars().all()
        records = (
            session.execute(
                select(IdempotencyRecord).where(IdempotencyRecord.scope == "upload_finalise")
            )
            .scalars()
            .all()
        )
    assert len(documents) == 1
    assert len(jobs) == 1
    assert len(records) == 1
    assert records[0].status == "SUCCEEDED"


@pytest.mark.timeout(120)
def test_pipeline_and_reports_idempotency_and_tenant_isolation(tmp_path, monkeypatch) -> None:
    testing_session_local = _setup_sqlite(tmp_path, monkeypatch)
    with testing_session_local() as session:
        _seed_taxonomy(session)

    monkeypatch.setattr(
        preprocess_service,
        "convert_from_bytes",
        lambda payload, dpi, fmt: [Image.new("RGB", (640, 480), color="white")],
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: (
            "Invoice ref INV-001\n"
            "Invoice date 2025-01-01\n"
            "Supplier name Acme Packaging\n"
            "Product description household primary plastic bottle\n"
            "brand household primary\n"
            "1000 g"
        ),
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_data",
        lambda image, config, output_type=None: (
            {
                "level": [5, 5],
                "page_num": [1, 1],
                "block_num": [1, 1],
                "par_num": [1, 1],
                "line_num": [1, 1],
                "word_num": [1, 2],
                "left": [10, 40],
                "top": [10, 10],
                "width": [20, 20],
                "height": [10, 10],
                "conf": ["90", "92"],
                "text": ["INV-001", "Plastic"],
            }
            if output_type is not None
            else (
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
                "width\theight\tconf\ttext\n"
                "5\t1\t1\t1\t1\t1\t10\t10\t20\t10\t90\tINV-001\n"
                "5\t1\t1\t1\t1\t2\t40\t10\t20\t10\t92\tPlastic\n"
            )
        ),
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_pdf_or_hocr",
        lambda image, extension, config: b"<html>hocr</html>",
    )

    class FakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            return None

    monkeypatch.setattr(documents_router, "JobQueue", FakeJobQueue)

    client = TestClient(app)
    tenant_a_headers = {"X-User-Id": "alice", "X-Tenant-Id": "123456"}
    tenant_b_headers = {"X-User-Id": "bob", "X-Tenant-Id": "999999"}

    file_bytes = b"%PDF-1.4\n%pipeline"
    presign = client.post(
        "/api/v1/documents/upload/presign",
        headers=tenant_a_headers,
        json={
            "filename": "pipeline.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(file_bytes),
        },
    )
    assert presign.status_code == 200
    presigned = presign.json()
    ObjectStorage().put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=file_bytes,
        content_type="application/pdf",
    )

    finalise = client.post(
        "/api/v1/documents/upload/finalise",
        headers={**tenant_a_headers, "Idempotency-Key": "upload-finalise-2"},
        json={"upload_id": presigned["upload_id"]},
    )
    assert finalise.status_code == 200
    document_id = finalise.json()["document_id"]

    first_run = client.post(
        f"/api/v1/pipeline/run/{document_id}",
        headers={**tenant_a_headers, "Idempotency-Key": "pipeline-run-1"},
    )
    second_run = client.post(
        f"/api/v1/pipeline/run/{document_id}",
        headers={**tenant_a_headers, "Idempotency-Key": "pipeline-run-1"},
    )
    assert first_run.status_code == 200
    assert second_run.status_code == 200
    assert second_run.json() == first_run.json()

    cross_tenant_run = client.post(
        f"/api/v1/pipeline/run/{document_id}",
        headers={**tenant_b_headers, "Idempotency-Key": "pipeline-run-x"},
    )
    assert cross_tenant_run.status_code == 404

    report_id = first_run.json()["report_id"]
    cross_tenant_download = client.get(
        f"/api/v1/reports/{report_id}/download",
        headers=tenant_b_headers,
    )
    assert cross_tenant_download.status_code == 404

    first_export = client.post(
        f"/api/v1/reports/{report_id}/export",
        headers={**tenant_a_headers, "Idempotency-Key": "report-export-1"},
    )
    second_export = client.post(
        f"/api/v1/reports/{report_id}/export",
        headers={**tenant_a_headers, "Idempotency-Key": "report-export-1"},
    )
    assert first_export.status_code == 200
    assert second_export.status_code == 200
    assert second_export.json() == first_export.json()

    cross_tenant_export = client.post(
        f"/api/v1/reports/{report_id}/export",
        headers={**tenant_b_headers, "Idempotency-Key": "report-export-x"},
    )
    assert cross_tenant_export.status_code == 404

    with testing_session_local() as session:
        report = session.get(Report, UUID(report_id))
        assert report is not None
        assert report.status == "generated"

        classifications = (
            session.execute(
                select(Classification).where(Classification.document_id == UUID(document_id))
            )
            .scalars()
            .all()
        )
        idempotency_rows = session.execute(select(IdempotencyRecord)).scalars().all()

    assert len(classifications) == 1
    assert any(
        row.scope == "pipeline_run" and row.idempotency_key == "pipeline-run-1"
        for row in idempotency_rows
    )
    assert any(
        row.scope == "report_export" and row.idempotency_key == "report-export-1"
        for row in idempotency_rows
    )

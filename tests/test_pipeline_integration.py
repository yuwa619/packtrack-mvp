from __future__ import annotations

import csv
from io import StringIO
from uuid import UUID

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import api.app.db.session as db_session_module
from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import (
    AuditEvent,
    Classification,
    Document,
    Entity,
    Page,
    Report,
    ReviewTask,
    TaxonomyCode,
)
from api.app.main import app
from api.app.routers import documents as documents_router
from api.app.schemas.defra import DEFRA_REPORT_COLUMNS
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


def test_single_job_pipeline_completes_and_exports_defra_csv(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-test.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    with testing_session_local() as session:
        _seed_taxonomy(session)
    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)
    monkeypatch.setattr(
        preprocess_service,
        "convert_from_bytes",
        lambda payload, dpi, fmt: [Image.new("RGB", (640, 480), color="white")],
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: "123456 SB HH P1 Paper or cardboard",
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
                "conf": ["55", "92"],
                "text": ["123456", "Paper"],
            }
            if output_type is not None
            else (
                "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\t"
                "width\\theight\\tconf\\ttext\\n"
                "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t20\\t10\\t55\\t123456\\n"
                "5\\t1\\t1\\t1\\t1\\t2\\t40\\t10\\t20\\t10\\t92\\tPaper\\n"
            )
        ),
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_pdf_or_hocr",
        lambda image, extension, config: b"<html>hocr</html>",
    )

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class FakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", FakeJobQueue)

    client = TestClient(app)
    auth_headers = {"X-User-Id": "test-user", "X-Tenant-Id": "123456"}

    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers=auth_headers,
        json={
            "filename": "sample.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(b"%PDF-1.4\n%stub"),
        },
    )
    assert presign_response.status_code == 200
    presigned = presign_response.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=b"%PDF-1.4\n%stub",
        content_type="application/pdf",
    )

    finalise_response = client.post(
        "/api/v1/documents/upload/finalise",
        headers=auth_headers,
        json={"upload_id": presigned["upload_id"]},
    )
    assert finalise_response.status_code == 200
    document_id = finalise_response.json()["document_id"]
    assert len(enqueued) == 1

    run_response = client.post(f"/api/v1/pipeline/run/{document_id}", headers=auth_headers)
    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["status"] == "COMPLETE"
    assert payload["review_task_count"] >= 2

    report_id = payload["report_id"]
    download_response = client.get(f"/api/v1/reports/{report_id}/download", headers=auth_headers)
    assert download_response.status_code == 200

    reader = csv.reader(StringIO(download_response.text))
    rows = list(reader)
    assert rows[0] == DEFRA_REPORT_COLUMNS

    with testing_session_local() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.status == "COMPLETE"

        report = session.get(Report, UUID(report_id))
        assert report is not None
        assert report.output_path is not None
        assert report.output_path.startswith("minio://reports/")

        pages = session.execute(select(Page).where(Page.document_id == document.id)).scalars().all()
        entities = (
            session.execute(
                select(Entity)
                .join(Page, Entity.page_id == Page.id)
                .where(Page.document_id == document.id)
            )
            .scalars()
            .all()
        )
        classifications = (
            session.execute(select(Classification).where(Classification.document_id == document.id))
            .scalars()
            .all()
        )
        review_tasks = (
            session.execute(select(ReviewTask).where(ReviewTask.document_id == document.id))
            .scalars()
            .all()
        )
        audit_events = (
            session.execute(select(AuditEvent).where(AuditEvent.entity_id == str(document.id)))
            .scalars()
            .all()
        )

    assert len(pages) == 1
    assert len(entities) >= 1
    assert len(classifications) == 1
    assert len(review_tasks) >= 2

    event_types = {event.event_type for event in audit_events}
    assert "STAGE_STARTED" in event_types
    assert "STAGE_FINISHED" in event_types
    assert "PREPROCESS_STAGE_FINISHED" in event_types
    assert "EXTRACT_STAGE_FINISHED" in event_types
    assert "CLASSIFY_STAGE_FINISHED" in event_types
    assert "REPORT_STAGE_FINISHED" in event_types
    assert "PIPELINE_RUN_COMPLETED" in event_types

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
from api.app.db.models import AuditEvent, Document, Report, ReviewTask, TaxonomyCode
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


def test_review_corrections_rerun_downstream_and_generate_new_report(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-review.db"
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
        lambda image, config: (
            "Invoice ref INV-001\n"
            "Invoice date 01/01/2025\n"
            "Supplier name Acme Packaging\n"
            "Product description plastic and paper containers\n"
            "brand household primary\n"
            "1200 g"
        ),
    )

    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_data",
        lambda image, config, output_type=None: (
            {
                "level": [5, 5, 5],
                "page_num": [1, 1, 1],
                "block_num": [1, 1, 1],
                "par_num": [1, 1, 1],
                "line_num": [1, 1, 1],
                "word_num": [1, 2, 3],
                "left": [10, 40, 90],
                "top": [10, 10, 10],
                "width": [20, 20, 20],
                "height": [10, 10, 10],
                "conf": ["58", "90", "93"],
                "text": ["INV-001", "plastic", "paper"],
            }
            if output_type is not None
            else (
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
                "width\theight\tconf\ttext\n"
                "5\t1\t1\t1\t1\t1\t10\t10\t20\t10\t58\tINV-001\n"
                "5\t1\t1\t1\t1\t2\t40\t10\t20\t10\t90\tplastic\n"
                "5\t1\t1\t1\t1\t3\t90\t10\t20\t10\t93\tpaper\n"
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
    auth_headers = {"X-User-Id": "qa-user", "X-Tenant-Id": "123456"}

    file_bytes = b"%PDF-1.4\n%review"
    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers=auth_headers,
        json={
            "filename": "review-sample.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(file_bytes),
        },
    )
    assert presign_response.status_code == 200
    presigned = presign_response.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=file_bytes,
        content_type="application/pdf",
    )

    finalise_response = client.post(
        "/api/v1/documents/upload/finalise",
        headers=auth_headers,
        json={"upload_id": presigned["upload_id"]},
    )
    assert finalise_response.status_code == 200
    document_id = finalise_response.json()["document_id"]
    assert enqueued

    run_response = client.post(f"/api/v1/pipeline/run/{document_id}", headers=auth_headers)
    assert run_response.status_code == 200

    tasks_response = client.get("/api/v1/review/tasks?status=pending", headers=auth_headers)
    assert tasks_response.status_code == 200
    tasks = tasks_response.json()["tasks"]
    assert tasks

    target_task = next(
        (task for task in tasks if task["task_type"] == "CLASSIFICATION_REVIEW"),
        tasks[0],
    )

    detail_response = client.get(
        f"/api/v1/review/tasks/{target_task['task_id']}",
        headers=auth_headers,
    )
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["pages"]

    first_page_endpoint = detail["pages"][0]["image_endpoint"]
    image_response = client.get(first_page_endpoint, headers=auth_headers)
    assert image_response.status_code == 200
    assert image_response.headers["content-type"].startswith("image/png")

    candidates = detail["classification"]["candidates"]
    classification_choice = candidates[0] if candidates else None

    correction_payload: dict[str, object] = {
        "extracted_fields": [
            {"field_name": "invoice_ref", "value": "INV-REVIEW-001", "page_number": 1}
        ],
        "reviewer": "qa-user",
    }
    if classification_choice:
        correction_payload["classification_choice"] = {
            "category": classification_choice["category"],
            "code": classification_choice["code"],
        }

    correction_response = client.post(
        f"/api/v1/review/tasks/{target_task['task_id']}/corrections",
        headers=auth_headers,
        json=correction_payload,
    )
    assert correction_response.status_code == 200
    rerun_payload = correction_response.json()["rerun"]
    assert rerun_payload["status"] == "COMPLETE"
    assert rerun_payload["classification_reran"] is True

    remaining_tasks_response = client.get(
        "/api/v1/review/tasks?status=pending", headers=auth_headers
    )
    assert remaining_tasks_response.status_code == 200
    remaining_tasks = remaining_tasks_response.json()["tasks"]
    if remaining_tasks:
        complete_response = client.patch(
            f"/api/v1/review/tasks/{remaining_tasks[0]['task_id']}/complete",
            headers=auth_headers,
            json={"reviewer": "qa-user"},
        )
        assert complete_response.status_code == 200
        assert complete_response.json()["rerun"]["status"] == "COMPLETE"

    report_id = rerun_payload["report_id"]
    reports_response = client.get("/api/v1/reports", headers=auth_headers)
    assert reports_response.status_code == 200
    report_ids = {item["report_id"] for item in reports_response.json()["reports"]}
    assert report_id in report_ids

    download_response = client.get(f"/api/v1/reports/{report_id}/download", headers=auth_headers)
    assert download_response.status_code == 200
    csv_rows = list(csv.reader(StringIO(download_response.text)))
    assert csv_rows[0] == DEFRA_REPORT_COLUMNS

    with testing_session_local() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.status == "COMPLETE"

        task = session.get(ReviewTask, UUID(target_task["task_id"]))
        assert task is not None
        assert task.status == "resolved"

        report = session.get(Report, UUID(report_id))
        assert report is not None
        assert report.output_path is not None
        assert report.output_path.startswith("minio://reports/")

        event_types = {
            row.event_type for row in session.execute(select(AuditEvent)).scalars().all()
        }

    assert "REVIEW_CORRECTION_SUBMITTED" in event_types
    assert "PIPELINE_RERUN_COMPLETED" in event_types
    assert "REPORT_STAGE_FINISHED" in event_types

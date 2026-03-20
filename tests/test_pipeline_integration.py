from __future__ import annotations

import csv
import zipfile
from io import BytesIO, StringIO
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration
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
    DocumentMaterialClassification,
    Entity,
    Page,
    Report,
    ReviewTask,
    TaxonomyCode,
)
from api.app.main import app
from api.app.routers import batches as batches_router
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
        ("Material", "Aluminium", "Aluminium"),
        ("Material", "Steel", "Steel"),
        ("Material", "Glass", "Glass"),
        ("Material", "Wood", "Wood"),
        ("Material", "Other", "Other"),
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


def _setup_sqlite_test_db(tmp_path, monkeypatch, db_name: str):
    sqlite_path = tmp_path / db_name
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
    return testing_session_local


def _blank_png_bytes() -> bytes:
    image_buffer = BytesIO()
    Image.new("RGB", (640, 480), color="white").save(image_buffer, format="PNG")
    return image_buffer.getvalue()


def _configure_mock_ocr(monkeypatch, texts: list[str]) -> None:
    ocr_texts = iter(texts)
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: next(ocr_texts),
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
                "left": [10, 40, 70],
                "top": [10, 10, 10],
                "width": [20, 20, 20],
                "height": [10, 10, 10],
                "conf": ["95", "94", "93"],
                "text": ["Invoice", "UK", "2024"],
            }
            if output_type is not None
            else (
                "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\t"
                "width\\theight\\tconf\\ttext\\n"
                "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t20\\t10\\t95\\tInvoice\\n"
                "5\\t1\\t1\\t1\\t1\\t2\\t40\\t10\\t20\\t10\\t94\\tUK\\n"
                "5\\t1\\t1\\t1\\t1\\t3\\t70\\t10\\t20\\t10\\t93\\t2024\\n"
            )
        ),
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_pdf_or_hocr",
        lambda image, extension, config: b"<html>hocr</html>",
    )


class FakeJobQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, str | int]]] = []

    def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
        self.enqueued.append((queue_name, payload))


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


def test_pipeline_auto_material_detection_non_blocking_multi_row_export(
    tmp_path, monkeypatch
) -> None:
    sqlite_path = tmp_path / "packtrack-auto-materials.db"
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
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: (
            "Invoice ref APEX-1806\n"
            "Invoice date 06/03/2026\n"
            "Supplier name Apex Packaging\n"
            "Product description PET trays, LDPE film wrap, cardboard carton, "
            "aluminium foil, steel can, wood pallet, glass jar"
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
                "left": [10, 40, 70],
                "top": [10, 10, 10],
                "width": [20, 20, 20],
                "height": [10, 10, 10],
                "conf": ["90", "92", "88"],
                "text": ["APEX-1806", "PET", "cardboard"],
            }
            if output_type is not None
            else (
                "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\t"
                "width\\theight\\tconf\\ttext\\n"
                "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t20\\t10\\t90\\tAPEX-1806\\n"
                "5\\t1\\t1\\t1\\t1\\t2\\t40\\t10\\t20\\t10\\t92\\tPET\\n"
                "5\\t1\\t1\\t1\\t1\\t3\\t70\\t10\\t20\\t10\\t88\\tcardboard\\n"
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

    image_buffer = BytesIO()
    Image.new("RGB", (640, 480), color="white").save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()

    client = TestClient(app)
    auth_headers = {"X-User-Id": "test-user", "X-Tenant-Id": "123456"}

    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers=auth_headers,
        json={
            "filename": "IMG_1806.PNG",
            "mime_type": "image/png",
            "size_bytes": len(image_bytes),
        },
    )
    assert presign_response.status_code == 200
    presigned = presign_response.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=image_bytes,
        content_type="image/png",
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
    payload = run_response.json()
    assert payload["status"] == "COMPLETE"

    report_id = payload["report_id"]
    download_response = client.get(f"/api/v1/reports/{report_id}/download", headers=auth_headers)
    assert download_response.status_code == 200
    csv_rows = list(csv.reader(StringIO(download_response.text)))
    assert csv_rows[0] == DEFRA_REPORT_COLUMNS
    assert len(csv_rows) >= 6

    reports_response = client.get("/api/v1/reports", headers=auth_headers)
    assert reports_response.status_code == 200
    listed_report = next(
        report for report in reports_response.json()["reports"] if report["report_id"] == report_id
    )
    assert listed_report["warning_count"] > 0
    assert listed_report["validation_warnings"]["missing_fields_by_row"]

    with testing_session_local() as session:
        auto_material_rows = (
            session.execute(
                select(DocumentMaterialClassification).where(
                    DocumentMaterialClassification.document_id == UUID(document_id),
                    DocumentMaterialClassification.source == "auto",
                )
            )
            .scalars()
            .all()
        )
        assert len(auto_material_rows) >= 5

        report = session.get(Report, UUID(report_id))
        assert report is not None
        warnings = report.validation_warnings
        assert warnings["missing_fields_by_row"]
        missing_fields = {
            field for row in warnings["missing_fields_by_row"] for field in row["missing_fields"]
        }
        assert "packaging_material_weight" in missing_fields
        # packaging_material_units is a DEFRA numeric item count column that
        # PackTrack does not currently populate, so it is no longer flagged.

        material_event = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "document",
                    AuditEvent.entity_id == document_id,
                    AuditEvent.event_type == "MATERIALS_AUTO_DETECTED",
                )
            )
            .scalars()
            .first()
        )
        warnings_event = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "document",
                    AuditEvent.entity_id == document_id,
                    AuditEvent.event_type == "REPORT_WARNINGS_GENERATED",
                )
            )
            .scalars()
            .first()
        )

    assert material_event is not None
    assert warnings_event is not None


def test_batch_upload_run_and_combined_report_export(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-batch.db"
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

    ocr_texts = iter(
        [
            (
                "Invoice ref INVPET1001\n"
                "Invoice date 01/03/2026\n"
                "Supplier name North PET Packaging Ltd\n"
                "Product description PET trays"
            ),
            (
                "Invoice ref GL2042\n"
                "Invoice date 02/03/2026\n"
                "Supplier name Glassworks UK Ltd\n"
                "Product description glass jar"
            ),
            (
                "Invoice ref ALU7788\n"
                "Invoice date 03/03/2026\n"
                "Supplier name AluCan Imports\n"
                "Product description aluminium can"
            ),
        ]
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: next(ocr_texts),
    )
    # Each document's image_to_data tokens must match its image_to_string text
    # to avoid false material detections from stale/shared tokens.
    # image_to_data is called twice per page (TSV then dict), so we advance
    # to the next document's tokens every two calls.
    _per_doc_tokens = [
        ["INVOICE", "PET", "trays"],
        ["INVOICE", "glass", "jar"],
        ["INVOICE", "aluminium", "can"],
    ]
    _data_call_count = [0]

    def _fake_image_to_data(image, config, output_type=None):
        doc_index = _data_call_count[0] // 2
        _data_call_count[0] += 1
        tokens = _per_doc_tokens[doc_index % len(_per_doc_tokens)]
        n = len(tokens)
        if output_type is not None:
            return {
                "level": [5] * n,
                "page_num": [1] * n,
                "block_num": [1] * n,
                "par_num": [1] * n,
                "line_num": [1] * n,
                "word_num": list(range(1, n + 1)),
                "left": [10 + 30 * i for i in range(n)],
                "top": [10] * n,
                "width": [20] * n,
                "height": [10] * n,
                "conf": ["95"] * n,
                "text": tokens,
            }
        header = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
            "\tleft\ttop\twidth\theight\tconf\ttext\n"
        )
        rows = "".join(
            f"5\t1\t1\t1\t1\t{i+1}\t{10+30*i}\t10\t20\t10\t95\t{tok}\n"
            for i, tok in enumerate(tokens)
        )
        return header + rows

    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_data",
        _fake_image_to_data,
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
    monkeypatch.setattr(batches_router, "JobQueue", FakeJobQueue)

    source_fixture_root = Path(__file__).parent / "fixtures" / "invoices"
    fixture_names = [
        "invoice_table_top_left.pdf",
        "invoice_right_header_glass.pdf",
        "invoice_lines_aluminium.pdf",
    ]
    fixture_payloads: list[tuple[str, bytes]] = []
    for fixture_name in fixture_names:
        fixture_bytes = (source_fixture_root / fixture_name).read_bytes()
        fixture_payloads.append((fixture_name, fixture_bytes))

    client = TestClient(app)
    auth_headers = {"X-User-Id": "test-user", "X-Tenant-Id": "123456"}

    create_response = client.post(
        "/api/v1/batches",
        headers=auth_headers,
        json={
            "name": "Fixture batch",
            "files": [
                {
                    "filename": name,
                    "mime_type": "application/pdf",
                    "size_bytes": len(payload),
                }
                for name, payload in fixture_payloads
            ],
        },
    )
    assert create_response.status_code == 200
    batch_payload = create_response.json()
    assert len(batch_payload["uploads"]) == 3

    storage = ObjectStorage()
    for upload, (_name, payload) in zip(batch_payload["uploads"], fixture_payloads, strict=True):
        storage.put_bytes(
            bucket=upload["bucket"],
            key=upload["object_key"],
            data=payload,
            content_type="application/pdf",
        )

    finalise_response = client.post(
        f"/api/v1/batches/{batch_payload['batch_id']}/finalise",
        headers=auth_headers,
        json={"upload_ids": [item["upload_id"] for item in batch_payload["uploads"]]},
    )
    assert finalise_response.status_code == 200
    finalise_payload = finalise_response.json()
    assert len(finalise_payload["document_ids"]) == 3

    run_response = client.post(
        f"/api/v1/batches/{batch_payload['batch_id']}/run",
        headers=auth_headers,
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["status"] == "COMPLETE"
    assert len(run_payload["results"]) == 3
    assert all(item["status"] == "COMPLETE" for item in run_payload["results"])

    export_response = client.post(
        f"/api/v1/batches/{batch_payload['batch_id']}/reports/export",
        headers=auth_headers,
    )
    assert export_response.status_code == 200
    export_payload = export_response.json()
    assert export_payload["row_count"] == 3
    assert export_payload["warning_count"] > 0
    assert export_payload["validation_warnings"]["missing_fields_by_row"]
    assert export_payload["validation_warnings"]["per_document"]

    report_id = export_payload["report_id"]
    download_response = client.get(f"/api/v1/reports/{report_id}/download", headers=auth_headers)
    assert download_response.status_code == 200
    rows = list(csv.reader(StringIO(download_response.text)))
    assert rows[0] == DEFRA_REPORT_COLUMNS
    assert len(rows) == 4

    reports_response = client.get("/api/v1/reports", headers=auth_headers)
    assert reports_response.status_code == 200
    batch_report = next(
        report for report in reports_response.json()["reports"] if report["report_id"] == report_id
    )
    assert batch_report["report_scope"] == "batch"
    assert batch_report["document_count"] == 3

    # Verify expected rows per document: 1 material per invoice
    with testing_session_local() as session:
        for doc_id in finalise_payload["document_ids"]:
            doc = session.get(Document, UUID(doc_id))
            mat_rows = (
                session.execute(
                    select(DocumentMaterialClassification).where(
                        DocumentMaterialClassification.document_id == doc.id
                    )
                )
                .scalars()
                .all()
            )
            assert len(mat_rows) == 1, (
                f"{doc.original_filename} should have exactly 1 material row, got {len(mat_rows)}"
            )

    # Verify CSV data rows contain the expected 3 distinct materials
    data_materials = {row[7] for row in rows[1:]}  # packaging_material column (index 7)
    assert "Plastic" in data_materials
    assert "Glass" in data_materials
    assert "Aluminium" in data_materials
    assert len(data_materials) == 3

    with testing_session_local() as session:
        report = session.get(Report, UUID(report_id))
        assert report is not None
        assert report.batch_id is not None
        assert report.document_id is None
        assert report.output_path is not None
        assert report.output_path.startswith("minio://reports/")


def test_zip_batch_upload_finalise_run_and_combined_report_export(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-batch-zip.db"
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

    ocr_texts = iter(
        [
            (
                "Invoice ref ZIPPET1001\n"
                "Invoice date 04/03/2026\n"
                "Supplier name North PET Packaging Ltd\n"
                "Product description PET trays"
            ),
            (
                "Invoice ref ZIPGLASS2002\n"
                "Invoice date 05/03/2026\n"
                "Supplier name Glassworks UK Ltd\n"
                "Product description glass jar"
            ),
        ]
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: next(ocr_texts),
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
                "left": [10, 40, 70],
                "top": [10, 10, 10],
                "width": [20, 20, 20],
                "height": [10, 10, 10],
                "conf": ["95", "94", "93"],
                "text": ["Invoice", "Ref", "2026"],
            }
            if output_type is not None
            else (
                "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\t"
                "width\\theight\\tconf\\ttext\\n"
                "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t20\\t10\\t95\\tInvoice\\n"
                "5\\t1\\t1\\t1\\t1\\t2\\t40\\t10\\t20\\t10\\t94\\tRef\\n"
                "5\\t1\\t1\\t1\\t1\\t3\\t70\\t10\\t20\\t10\\t93\\t2026\\n"
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
    monkeypatch.setattr(batches_router, "JobQueue", FakeJobQueue)

    source_fixture_root = Path(__file__).parent / "fixtures" / "invoices"
    first_pdf = (source_fixture_root / "invoice_table_top_left.pdf").read_bytes()
    second_pdf = (source_fixture_root / "invoice_right_header_glass.pdf").read_bytes()

    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w") as archive:
        archive.writestr("invoice_table_top_left.pdf", first_pdf)
        archive.writestr("folder/invoice_right_header_glass.pdf", second_pdf)
        archive.writestr("notes.txt", b"operator notes")
        archive.writestr("../sneaky.pdf", first_pdf)
    zip_bytes = zip_buffer.getvalue()

    client = TestClient(app)
    auth_headers = {"X-User-Id": "test-user", "X-Tenant-Id": "123456"}

    presign_response = client.post(
        "/api/v1/batches/upload-zip/presign",
        headers=auth_headers,
        json={
            "filename": "fixture_batch.zip",
            "mime_type": "application/zip",
            "size_bytes": len(zip_bytes),
            "name": "ZIP fixture batch",
        },
    )
    assert presign_response.status_code == 200
    presign_payload = presign_response.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=settings.minio_bucket_raw,
        key=f"raw-uploads/123456/batches/{presign_payload['batch_id']}/{presign_payload['upload_id']}/fixture_batch.zip",
        data=zip_bytes,
        content_type="application/zip",
    )

    finalise_response = client.post(
        f"/api/v1/batches/{presign_payload['batch_id']}/finalise-zip",
        headers=auth_headers,
        json={"upload_id": presign_payload["upload_id"]},
    )
    assert finalise_response.status_code == 200
    finalise_payload = finalise_response.json()
    assert finalise_payload["accepted_count"] == 2
    assert finalise_payload["rejected_count"] == 2
    assert len(finalise_payload["accepted_files"]) == 2
    rejected_reasons = {
        item["filename"]: item["reason"] for item in finalise_payload["rejected_files"]
    }
    assert rejected_reasons["notes.txt"] == "Unsupported file type"
    assert rejected_reasons["../sneaky.pdf"] == "Path traversal entry is not allowed"

    run_response = client.post(
        f"/api/v1/batches/{presign_payload['batch_id']}/run",
        headers=auth_headers,
    )
    assert run_response.status_code == 200
    run_payload = run_response.json()
    assert run_payload["status"] == "COMPLETE"
    assert len(run_payload["results"]) == 2
    assert all(item["status"] == "COMPLETE" for item in run_payload["results"])

    export_response = client.post(
        f"/api/v1/batches/{presign_payload['batch_id']}/reports/export",
        headers=auth_headers,
    )
    assert export_response.status_code == 200
    export_payload = export_response.json()
    assert export_payload["warning_count"] > 0
    assert export_payload["validation_warnings"]["missing_fields_by_row"]

    report_id = export_payload["report_id"]
    download_response = client.get(f"/api/v1/reports/{report_id}/download", headers=auth_headers)
    assert download_response.status_code == 200
    rows = list(csv.reader(StringIO(download_response.text)))
    assert rows[0] == DEFRA_REPORT_COLUMNS
    assert len(enqueued) >= 2

    accepted_document_ids = [
        UUID(item["document_id"]) for item in finalise_payload["accepted_files"]
    ]
    with testing_session_local() as session:
        material_row_count = (
            session.execute(
                select(DocumentMaterialClassification).where(
                    DocumentMaterialClassification.document_id.in_(accepted_document_ids)
                )
            )
            .scalars()
            .all()
        )
    assert export_payload["row_count"] == len(material_row_count)
    assert len(rows) == len(material_row_count) + 1


def test_invoice_document_metadata_country_and_multi_material_rows(tmp_path, monkeypatch) -> None:
    testing_session_local = _setup_sqlite_test_db(
        tmp_path, monkeypatch, "packtrack-invoice-metadata.db"
    )
    _configure_mock_ocr(
        monkeypatch,
        [
            (
                "PACKAGING INVOICE\n"
                "Invoice Ref: APEX-1806\n"
                "DATE: OCT 28, 2024\n"
                "Bill To: Apex Foods Ltd, London, UK\n"
                "Product Description: PET trays, cardboard sleeves, glass jars\n"
            )
        ],
    )

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class LocalFakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", LocalFakeJobQueue)

    image_bytes = _blank_png_bytes()
    client = TestClient(app)
    auth_headers = {"X-User-Id": "test-user", "X-Tenant-Id": "123456"}

    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers=auth_headers,
        json={
            "filename": "apex_invoice.png",
            "mime_type": "image/png",
            "size_bytes": len(image_bytes),
        },
    )
    assert presign_response.status_code == 200
    presigned = presign_response.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=image_bytes,
        content_type="image/png",
    )

    finalise_response = client.post(
        "/api/v1/documents/upload/finalise",
        headers=auth_headers,
        json={"upload_id": presigned["upload_id"]},
    )
    assert finalise_response.status_code == 200
    document_id = finalise_response.json()["document_id"]
    run_response = client.post(f"/api/v1/pipeline/run/{document_id}", headers=auth_headers)
    assert run_response.status_code == 200
    report_id = run_response.json()["report_id"]

    with testing_session_local() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.document_type == "commercial_packaging_invoice"
        assert document.document_date == "2024-10-28"
        assert document.inferred_country_code == "GB"

        material_rows = (
            session.execute(
                select(DocumentMaterialClassification).where(
                    DocumentMaterialClassification.document_id == document.id
                )
            )
            .scalars()
            .all()
        )
        assert len(material_rows) >= 3
        exported_materials = {row.packaging_material for row in material_rows}
        assert {"Plastic", "Paper or cardboard", "Glass"}.issubset(exported_materials)

        report = session.get(Report, UUID(report_id))
        assert report is not None
        assert report.validation_warnings["document_metadata"]["document_date"] == "2024-10-28"
        assert (
            report.validation_warnings["document_metadata"]["country_inference"]["country_code"]
            == "GB"
        )
        assert "submission_period defaulted to 2025-P1" in " ".join(
            report.validation_warnings["overall"]
        )

    assert enqueued


def test_notice_of_liability_extracts_tonnage_rows_and_glass(tmp_path, monkeypatch) -> None:
    testing_session_local = _setup_sqlite_test_db(
        tmp_path, monkeypatch, "packtrack-packuk-notice.db"
    )
    _configure_mock_ocr(
        monkeypatch,
        [
            (
                "PACKUK me ots 24\n"
                "Cave 02031 2024\n"
                "NOTICE OF LIABILITY (NoL)\n"
                "Produce 10 SAM>.03001\n"
                "Name: ACME MANUFACTURING LTD\n"
                "1 2024 Compliance Year |\n"
                "toe Ca culation Beeacdown\n"
                "Hes a1 Category Tonnage Foe For Rate Amount\n"
                "Pin 300 tx £0600 pertoane = E1696\n"
                "\" Concbowrd 2300 Ze? £*50 00 pertome £229\n"
                "Gass ard Pat £°S2 00 per toane 1s\n"
                "Auer am “00 et EOC OD perio CG AS\n"
            )
        ],
    )

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class LocalFakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", LocalFakeJobQueue)

    image_bytes = _blank_png_bytes()
    client = TestClient(app)
    auth_headers = {"X-User-Id": "test-user", "X-Tenant-Id": "123456"}

    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers=auth_headers,
        json={
            "filename": "packuk_notice.png",
            "mime_type": "image/png",
            "size_bytes": len(image_bytes),
        },
    )
    assert presign_response.status_code == 200
    presigned = presign_response.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=image_bytes,
        content_type="image/png",
    )

    finalise_response = client.post(
        "/api/v1/documents/upload/finalise",
        headers=auth_headers,
        json={"upload_id": presigned["upload_id"]},
    )
    assert finalise_response.status_code == 200
    document_id = finalise_response.json()["document_id"]
    run_response = client.post(f"/api/v1/pipeline/run/{document_id}", headers=auth_headers)
    assert run_response.status_code == 200
    report_id = run_response.json()["report_id"]

    with testing_session_local() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.document_type == "notice_of_liability"
        assert document.document_date == "2024-03-02"
        assert document.inferred_country_code == "GB"

        material_rows = (
            session.execute(
                select(DocumentMaterialClassification).where(
                    DocumentMaterialClassification.document_id == document.id
                )
            )
            .scalars()
            .all()
        )
        # NoL should create exactly 4 material rows: Plastic, Paper/cardboard, Glass, Aluminium
        assert len(material_rows) == 4, (
            f"Expected 4 material rows for NoL, got {len(material_rows)}"
        )
        material_lookup = {
            row.packaging_material: (
                str(row.packaging_material_weight),
                row.weight_display_unit,
            )
            for row in material_rows
        }
        assert set(material_lookup.keys()) == {
            "Plastic", "Paper or cardboard", "Glass", "Aluminium"
        }
        # Weights are normalised from source tonnes to canonical kg.
        assert float(material_lookup["Plastic"][0]) == 3000.0
        assert material_lookup["Plastic"][1] == "kg"
        assert float(material_lookup["Paper or cardboard"][0]) == 23000.0
        assert material_lookup["Paper or cardboard"][1] == "kg"
        assert float(material_lookup["Glass"][0]) == 0.0
        assert material_lookup["Glass"][1] == "kg"
        assert float(material_lookup["Aluminium"][0]) == 1000.0
        assert material_lookup["Aluminium"][1] == "kg"

        report = session.get(Report, UUID(report_id))
        assert report is not None
        assert report.validation_warnings["document_metadata"]["document_date"] == "2024-03-02"
        assert report.validation_warnings["document_metadata"]["country_inference"] == {
            "country_code": "GB",
            "source": "inferred_from_text",
        }
        assert any(
            "PackUK tonnage extracted from fee breakdown" in warning
            for warning in report.validation_warnings["overall"]
        )

    assert enqueued


def test_combined_report_includes_apex_and_packuk_rows_with_country_and_weights(
    tmp_path, monkeypatch
) -> None:
    testing_session_local = _setup_sqlite_test_db(
        tmp_path, monkeypatch, "packtrack-combined-doc-types.db"
    )
    _configure_mock_ocr(
        monkeypatch,
        [
            (
                "PACKAGING INVOICE\n"
                "Invoice Ref: APEX-1806\n"
                "DATE: OCT 28, 2024\n"
                "Bill To: Apex Foods Ltd, London, UK\n"
                "Product Description: PET trays, cardboard sleeves, glass jars\n"
            ),
            (
                "PACKUK me ots 24\n"
                "Cave 02031 2024\n"
                "NOTICE OF LIABILITY (NoL)\n"
                "Produce 10 SAM>.03001\n"
                "Name: ACME MANUFACTURING LTD\n"
                "1 2024 Compliance Year |\n"
                "toe Ca culation Beeacdown\n"
                "Hes a1 Category Tonnage Foe For Rate Amount\n"
                "Pin 300 tx £0600 pertoane = E1696\n"
                "\" Concbowrd 2300 Ze? £*50 00 pertome £229\n"
                "Gass ard Pat £°S2 00 per toane 1s\n"
                "Auer am “00 et EOC OD perio CG AS\n"
            ),
        ],
    )

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class LocalFakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", LocalFakeJobQueue)
    monkeypatch.setattr(batches_router, "JobQueue", LocalFakeJobQueue)

    image_bytes = _blank_png_bytes()
    client = TestClient(app)
    auth_headers = {"X-User-Id": "test-user", "X-Tenant-Id": "123456"}

    create_response = client.post(
        "/api/v1/batches",
        headers=auth_headers,
        json={
            "name": "Apex + PackUK",
            "files": [
                {
                    "filename": "apex_invoice.png",
                    "mime_type": "image/png",
                    "size_bytes": len(image_bytes),
                },
                {
                    "filename": "packuk_notice.png",
                    "mime_type": "image/png",
                    "size_bytes": len(image_bytes),
                },
            ],
        },
    )
    assert create_response.status_code == 200
    batch_payload = create_response.json()

    storage = ObjectStorage()
    for upload in batch_payload["uploads"]:
        storage.put_bytes(
            bucket=upload["bucket"],
            key=upload["object_key"],
            data=image_bytes,
            content_type="image/png",
        )

    finalise_response = client.post(
        f"/api/v1/batches/{batch_payload['batch_id']}/finalise",
        headers=auth_headers,
        json={"upload_ids": [item["upload_id"] for item in batch_payload["uploads"]]},
    )
    assert finalise_response.status_code == 200
    finalise_payload = finalise_response.json()

    run_response = client.post(
        f"/api/v1/batches/{batch_payload['batch_id']}/run",
        headers=auth_headers,
    )
    assert run_response.status_code == 200
    export_response = client.post(
        f"/api/v1/batches/{batch_payload['batch_id']}/reports/export",
        headers=auth_headers,
    )
    assert export_response.status_code == 200
    export_payload = export_response.json()

    download_response = client.get(
        f"/api/v1/reports/{export_payload['report_id']}/download",
        headers=auth_headers,
    )
    assert download_response.status_code == 200
    rows = list(csv.DictReader(StringIO(download_response.text)))

    with testing_session_local() as session:
        documents = {
            str(document.id): document
            for document in session.execute(
                select(Document).where(
                    Document.id.in_(
                        [UUID(item) for item in finalise_payload["document_ids"]]
                    )
                )
            )
            .scalars()
            .all()
        }

    assert {document.original_filename for document in documents.values()} == {
        "apex_invoice.png",
        "packuk_notice.png",
    }
    assert all(row["from_country"] == "GB" and row["to_country"] == "GB" for row in rows)
    assert any(row["packaging_material"] == "Glass" for row in rows)
    # Glass may have weight from notice extraction (0 kg) or no weight from keyword detection.
    glass_rows = [row for row in rows if row["packaging_material"] == "Glass"]
    assert glass_rows
    # Notice-of-liability tonnage is normalised to kg (23 tonnes → 23000 kg).
    paper_rows = [
        row for row in rows
        if row["packaging_material"] == "Paper or cardboard"
        and row["packaging_material_weight"]
    ]
    assert paper_rows
    assert any(float(row["packaging_material_weight"]) == 23000.0 for row in paper_rows)
    assert export_payload["validation_warnings"]["per_document"]
    assert any(
        entry["document_date"] == "2024-10-28"
        for entry in export_payload["validation_warnings"]["per_document"]
    )
    assert any(
        entry["document_date"] == "2024-03-02"
        for entry in export_payload["validation_warnings"]["per_document"]
    )
    assert any(
        entry["country_inference"] == {"country_code": "GB", "source": "inferred_from_text"}
        for entry in export_payload["validation_warnings"]["per_document"]
    )
    assert any(
        "submission_period defaulted to 2025-P1" in warning
        for warning in export_payload["validation_warnings"]["overall"]
        + [
            item_warning
            for entry in export_payload["validation_warnings"]["per_document"]
            for item_warning in entry["overall"]
        ]
    )
    assert enqueued

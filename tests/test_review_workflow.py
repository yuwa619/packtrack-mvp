from __future__ import annotations

import csv
import json
from io import BytesIO, StringIO
from uuid import UUID, uuid4

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
    Document,
    DocumentMaterialClassification,
    Report,
    ReviewTask,
    TaxonomyCode,
    TrainingSample,
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
                "level": [5, 5, 5, 5, 5],
                "page_num": [1, 1, 1, 1, 1],
                "block_num": [1, 1, 1, 1, 1],
                "par_num": [1, 1, 1, 1, 1],
                "line_num": [1, 1, 1, 1, 1],
                "word_num": [1, 2, 3, 4, 5],
                "left": [10, 50, 90, 140, 210],
                "top": [10, 10, 10, 10, 10],
                "width": [32, 24, 44, 52, 40],
                "height": [10, 10, 10, 10, 10],
                "conf": ["58", "92", "90", "93", "91"],
                "text": ["Invoice", "ref", "INV-001", "plastic", "paper"],
            }
            if output_type is not None
            else (
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
                "width\theight\tconf\ttext\n"
                "5\t1\t1\t1\t1\t1\t10\t10\t32\t10\t58\tInvoice\n"
                "5\t1\t1\t1\t1\t2\t50\t10\t24\t10\t92\tref\n"
                "5\t1\t1\t1\t1\t3\t90\t10\t44\t10\t90\tINV-001\n"
                "5\t1\t1\t1\t1\t4\t140\t10\t52\t10\t93\tplastic\n"
                "5\t1\t1\t1\t1\t5\t210\t10\t40\t10\t91\tpaper\n"
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
    classification_choice = (
        candidates[0] if candidates else {"category": "Material", "code": "Plastic"}
    )

    correction_payload: dict[str, object] = {
        "extracted_fields": [
            {"field_name": "invoice_ref", "value": "INV-REVIEW-001", "page_number": 1}
        ],
        "classification_choice": {
            "category": classification_choice["category"],
            "code": classification_choice["code"],
        },
        "materials": [
            {
                "material": "Plastic",
                "subtype": "Rigid",
                "taxonomy_category": "Material",
                "taxonomy_code": "Plastic",
                "weight_value": 1.25,
                "weight_unit": "kg",
            },
            {
                "material": "Paper or cardboard",
                "subtype": None,
                "taxonomy_category": "Material",
                "taxonomy_code": "Paper or cardboard",
                "weight_value": 750,
                "weight_unit": "g",
            },
            {
                "material": "Glass",
                "subtype": None,
                "taxonomy_category": "Material",
                "taxonomy_code": "Glass",
                "weight_value": 2.1,
                "weight_unit": "kg",
            },
        ],
        "reviewer": "qa-user",
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
    assert len(csv_rows) == 4
    exported_materials = {row[7] for row in csv_rows[1:]}
    assert exported_materials == {"Plastic", "Paper or cardboard", "Glass"}
    # packaging_material_units (col 12) is a DEFRA numeric item-count column;
    # we do not populate it, so it must always be empty in the CSV.
    exported_units = {row[12] for row in csv_rows[1:]}
    assert exported_units == {""}

    with testing_session_local() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.status == "COMPLETE"

        task = session.get(ReviewTask, UUID(target_task["task_id"]))
        assert task is not None
        assert task.status == "resolved"

        training_sample = (
            session.execute(
                select(TrainingSample)
                .where(
                    TrainingSample.document_id == UUID(document_id),
                    TrainingSample.field_name == "invoice_ref",
                    TrainingSample.corrected_value == "INV-REVIEW-001",
                )
                .order_by(TrainingSample.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        assert training_sample is not None
        assert training_sample.page_number == 1
        assert training_sample.reviewer == "qa-user"
        assert training_sample.source == "field_correction"
        assert training_sample.ocr_text
        assert training_sample.span_start is not None
        assert training_sample.span_end is not None
        extracted_span = training_sample.ocr_text[
            training_sample.span_start : training_sample.span_end
        ]
        assert "INV-001" in extracted_span

        taxonomy_training_sample = (
            session.execute(
                select(TrainingSample)
                .where(
                    TrainingSample.document_id == UUID(document_id),
                    TrainingSample.field_name == "taxonomy_code",
                    TrainingSample.corrected_value == classification_choice["code"],
                    TrainingSample.taxonomy_code == classification_choice["code"],
                )
                .order_by(TrainingSample.created_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        assert taxonomy_training_sample is not None
        assert taxonomy_training_sample.reviewer == "qa-user"
        assert taxonomy_training_sample.source == "classification_override"
        assert taxonomy_training_sample.ocr_text
        assert taxonomy_training_sample.span_start is None
        assert taxonomy_training_sample.span_end is None

        report = session.get(Report, UUID(report_id))
        assert report is not None
        assert report.output_path is not None
        assert report.output_path.startswith("minio://reports/")
        assert report.row_count == 3

        material_rows = (
            session.execute(
                select(DocumentMaterialClassification).where(
                    DocumentMaterialClassification.document_id == UUID(document_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(material_rows) == 3

        # Verify reviewer-entered grams were normalised to kg
        paper_row = next(
            (r for r in material_rows if r.packaging_material == "Paper or cardboard"),
            None,
        )
        assert paper_row is not None
        assert float(paper_row.packaging_material_weight) == 0.75
        assert paper_row.weight_display_unit == "kg"

        event_types = {
            row.event_type for row in session.execute(select(AuditEvent)).scalars().all()
        }

    assert "REVIEW_CORRECTION_SUBMITTED" in event_types
    assert "PIPELINE_RERUN_COMPLETED" in event_types
    assert "REPORT_STAGE_FINISHED" in event_types


def test_materials_missing_weights_create_single_extraction_review_task(
    tmp_path, monkeypatch
) -> None:
    sqlite_path = tmp_path / "packtrack-material-weight-validation.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)

    document_id = uuid4()
    task_id = uuid4()

    with testing_session_local() as session:
        _seed_taxonomy(session)
        session.add(
            Document(
                id=document_id,
                organisation_id=123456,
                subsidiary_id="",
                organisation_size="L",
                submission_period="2025-P1",
                original_filename="materials-validation.png",
                mime_type="image/png",
                file_size_bytes=128,
                checksum_sha256="d" * 64,
                uploaded_by="qa-user",
                storage_path="minio://raw-uploads/tenant/materials-validation.png",
                status="COMPLETE",
            )
        )
        session.add(
            ReviewTask(
                id=task_id,
                document_id=document_id,
                classification_id=None,
                task_type="EXTRACTION_REVIEW",
                status="pending",
                notes="Validation task",
            )
        )
        session.commit()

    client = TestClient(app)
    auth_headers = {"X-User-Id": "qa-user", "X-Tenant-Id": "123456"}
    response = client.post(
        f"/api/v1/review/tasks/{task_id}/corrections",
        headers=auth_headers,
        json={
            "materials": [
                {
                    "material": "Plastic",
                    "subtype": "Rigid",
                    "taxonomy_category": "Material",
                    "taxonomy_code": "Plastic",
                    "weight_value": None,
                    "weight_unit": None,
                },
                {
                    "material": "Glass",
                    "subtype": None,
                    "taxonomy_category": "Material",
                    "taxonomy_code": "Glass",
                    "weight_value": 1.5,
                    "weight_unit": "kg",
                },
            ],
            "reviewer": "qa-user",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "resolved"
    assert payload["rerun"]["status"] == "COMPLETE"

    with testing_session_local() as session:
        tasks = (
            session.execute(
                select(ReviewTask).where(
                    ReviewTask.document_id == document_id,
                    ReviewTask.task_type == "EXTRACTION_REVIEW",
                    ReviewTask.status == "pending",
                    ReviewTask.notes.like("Optional: weight missing for %"),
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 1
        assert tasks[0].notes == "Optional: weight missing for Plastic Rigid"


def test_invalid_taxonomy_code_preserves_existing_materials(tmp_path, monkeypatch) -> None:
    """Invalid taxonomy code in correction must not delete existing materials."""
    sqlite_path = tmp_path / "packtrack-atomic-replace.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)

    document_id = uuid4()
    task_id = uuid4()

    with testing_session_local() as session:
        _seed_taxonomy(session)
        session.add(
            Document(
                id=document_id,
                organisation_id=123456,
                subsidiary_id="",
                organisation_size="L",
                submission_period="2025-P1",
                original_filename="atomic-test.png",
                mime_type="image/png",
                file_size_bytes=128,
                checksum_sha256="e" * 64,
                uploaded_by="qa-user",
                storage_path="minio://raw-uploads/tenant/atomic-test.png",
                status="COMPLETE",
            )
        )
        session.add(
            DocumentMaterialClassification(
                document_id=document_id,
                material_key="Plastic",
                taxonomy_category="Material",
                taxonomy_code="Plastic",
                packaging_material="Plastic",
                packaging_material_weight=1.0,
                weight_display_unit="kg",
                confidence=0.95,
                source="auto",
            )
        )
        session.add(
            ReviewTask(
                id=task_id,
                document_id=document_id,
                classification_id=None,
                task_type="EXTRACTION_REVIEW",
                status="pending",
                notes="Atomic test",
            )
        )
        session.commit()

    client = TestClient(app)
    auth_headers = {"X-User-Id": "qa-user", "X-Tenant-Id": "123456"}

    # Submit correction with invalid taxonomy code
    response = client.post(
        f"/api/v1/review/tasks/{task_id}/corrections",
        headers=auth_headers,
        json={
            "materials": [
                {
                    "material": "NonexistentMaterial",
                    "taxonomy_category": "Material",
                    "taxonomy_code": "DOES_NOT_EXIST",
                    "weight_value": 2.0,
                    "weight_unit": "kg",
                },
            ],
            "reviewer": "qa-user",
        },
    )
    assert response.status_code == 400

    # Existing material must still be intact
    with testing_session_local() as session:
        remaining = (
            session.execute(
                select(DocumentMaterialClassification).where(
                    DocumentMaterialClassification.document_id == document_id
                )
            )
            .scalars()
            .all()
        )
        assert len(remaining) == 1
        assert remaining[0].packaging_material == "Plastic"
        assert float(remaining[0].packaging_material_weight) == 1.0


def test_review_tasks_filter_by_document_id(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-review-filter.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)

    tenant_document_id = uuid4()
    other_document_id = uuid4()
    other_tenant_document_id = uuid4()

    with testing_session_local() as session:
        session.add_all(
            [
                Document(
                    id=tenant_document_id,
                    organisation_id=123456,
                    subsidiary_id="",
                    organisation_size="L",
                    submission_period="2025-P1",
                    original_filename="IMG_1806.PNG",
                    mime_type="image/png",
                    file_size_bytes=100,
                    checksum_sha256="a" * 64,
                    uploaded_by="qa-user",
                    storage_path="minio://raw-uploads/tenant/a.png",
                    status="COMPLETE",
                ),
                Document(
                    id=other_document_id,
                    organisation_id=123456,
                    subsidiary_id="",
                    organisation_size="L",
                    submission_period="2025-P1",
                    original_filename="other.png",
                    mime_type="image/png",
                    file_size_bytes=100,
                    checksum_sha256="b" * 64,
                    uploaded_by="qa-user",
                    storage_path="minio://raw-uploads/tenant/b.png",
                    status="COMPLETE",
                ),
                Document(
                    id=other_tenant_document_id,
                    organisation_id=654321,
                    subsidiary_id="",
                    organisation_size="L",
                    submission_period="2025-P1",
                    original_filename="other-tenant.png",
                    mime_type="image/png",
                    file_size_bytes=100,
                    checksum_sha256="c" * 64,
                    uploaded_by="qa-user",
                    storage_path="minio://raw-uploads/other/c.png",
                    status="COMPLETE",
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                ReviewTask(
                    document_id=tenant_document_id,
                    task_type="OCR_REVIEW",
                    status="pending",
                    notes="tenant target task",
                ),
                ReviewTask(
                    document_id=other_document_id,
                    task_type="EXTRACTION_REVIEW",
                    status="pending",
                    notes="tenant other task",
                ),
                ReviewTask(
                    document_id=tenant_document_id,
                    task_type="CLASSIFICATION_REVIEW",
                    status="resolved",
                    notes="resolved task should not appear in pending",
                ),
                ReviewTask(
                    document_id=other_tenant_document_id,
                    task_type="OCR_REVIEW",
                    status="pending",
                    notes="other tenant task",
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    auth_headers = {"X-User-Id": "qa-user", "X-Tenant-Id": "123456"}

    response = client.get(
        f"/api/v1/review/tasks?status=pending&document_id={tenant_document_id}",
        headers=auth_headers,
    )
    assert response.status_code == 200
    tasks = response.json()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["document_id"] == str(tenant_document_id)
    assert tasks[0]["filename"] == "IMG_1806.PNG"

    cross_tenant_response = client.get(
        f"/api/v1/review/tasks?status=pending&document_id={other_tenant_document_id}",
        headers=auth_headers,
    )
    assert cross_tenant_response.status_code == 200
    assert cross_tenant_response.json()["tasks"] == []


def test_img_1806_upload_limits_ocr_review_task_count(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-ocr-cap.db"
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
    monkeypatch.setattr(settings, "ocr_confidence_threshold", 0.70)

    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: (
            "INVOICE INV-1806\n"
            "DATE 2025-01-08\n"
            "Supplier Example Packaging Ltd\n"
            "PET trays 500 g\n"
            "cardboard sleeves 1 kg"
        ),
    )

    tsv_rows = [
        (1, 1, 1, ".", "10"),
        (1, 1, 2, "TO", "22"),
        (1, 1, 3, "INVOICE", "18"),
        (1, 1, 4, "INV-1806", "42"),
        (1, 1, 5, "Supplier", "88"),
        (1, 1, 6, "Example", "86"),
        (1, 1, 7, "Packaging", "84"),
        (1, 1, 8, "PET", "39"),
        (1, 1, 9, "500", "55"),
        (1, 1, 10, "g", "58"),
        (1, 1, 11, "cardboard", "61"),
        (1, 1, 12, "1", "57"),
        (1, 1, 13, "kg", "52"),
        (1, 1, 14, ",", "11"),
        (1, 1, 15, "()", "9"),
    ]

    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_data",
        lambda image, config, output_type=None: (
            {
                "level": [5] * len(tsv_rows),
                "page_num": [1] * len(tsv_rows),
                "block_num": [row[0] for row in tsv_rows],
                "par_num": [1] * len(tsv_rows),
                "line_num": [row[1] for row in tsv_rows],
                "word_num": [row[2] for row in tsv_rows],
                "left": [10 + (idx * 8) for idx in range(len(tsv_rows))],
                "top": [10] * len(tsv_rows),
                "width": [30] * len(tsv_rows),
                "height": [10] * len(tsv_rows),
                "conf": [row[4] for row in tsv_rows],
                "text": [row[3] for row in tsv_rows],
            }
            if output_type is not None
            else (
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
                "width\theight\tconf\ttext\n"
                + "\n".join(
                    (
                        f"5\t1\t{row[0]}\t1\t{row[1]}\t{row[2]}\t"
                        f"{10 + (idx * 8)}\t10\t30\t10\t{row[4]}\t{row[3]}"
                    )
                    for idx, row in enumerate(tsv_rows)
                )
                + "\n"
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
    auth_headers = {"X-User-Id": "qa-user", "X-Tenant-Id": "123456"}

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

    ObjectStorage().put_bytes(
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
    assert enqueued
    document_id = UUID(finalise_response.json()["document_id"])

    run_response = client.post(
        f"/api/v1/pipeline/run/{document_id}",
        headers=auth_headers,
    )
    assert run_response.status_code == 200
    assert run_response.json()["status"] == "COMPLETE"

    with testing_session_local() as session:
        ocr_tasks = (
            session.execute(
                select(ReviewTask).where(
                    ReviewTask.document_id == document_id,
                    ReviewTask.task_type == "OCR_REVIEW",
                )
            )
            .scalars()
            .all()
        )
        assert len(ocr_tasks) <= 3
        assert len(ocr_tasks) > 0

        for task in ocr_tasks:
            payload = json.loads(task.notes or "{}")
            assert "low_conf_token_count" in payload
            assert "examples" in payload
            assert "min_confidence" in payload
            assert "avg_confidence" in payload
            assert "ocr_artifact_uri" in payload

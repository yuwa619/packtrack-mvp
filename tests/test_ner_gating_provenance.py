from __future__ import annotations

import json
from datetime import timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import api.app.db.session as db_session_module
from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import AuditEvent, Document, Job, TaxonomyCode
from api.app.main import app
from api.app.routers import documents as documents_router
from api.app.services import ner_spacy as ner_spacy_service
from api.app.services import ocr as ocr_service
from api.app.services import preprocess as preprocess_service
from api.app.services.ner_registry import resolve_enabled_ner_model
from api.app.services.storage import ObjectStorage


def _seed_taxonomy(session) -> None:
    entries = [
        ("Packaging Activity", "SB", "Supplied under your brand"),
        ("Packaging Activity", "IM", "Imported"),
        ("Packaging Type", "HH", "Household packaging"),
        ("Packaging Type", "NH", "Non-household packaging"),
        ("Packaging Class", "P1", "Primary packaging"),
        ("Packaging Class", "P2", "Secondary packaging"),
        ("Material", "Paper or cardboard", "Paper or cardboard"),
        ("Material", "Plastic", "Plastic"),
        ("Material", "Glass", "Glass"),
        ("Plastic Sub-type", "Rigid", "Rigid plastic"),
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


def test_ner_quality_gate_blocks_low_quality_model(tmp_path) -> None:
    low_quality_registry = tmp_path / "latest-low.json"
    low_quality_registry.write_text(
        json.dumps(
            {
                "model_path": "data/models/spacy_ner/fake/model-best",
                "trained_at": "2026-03-05T14:00:00Z",
                "overall_f1": 0.59,
                "per_label_f1": {"INVOICE_REF": 0.95},
                "labels": ["INVOICE_REF"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="overall_f1"):
        resolve_enabled_ner_model(
            enabled=True,
            registry_path=low_quality_registry,
            min_overall_f1=0.60,
            min_invoice_ref_f1=0.90,
        )

    low_invoice_ref_registry = tmp_path / "latest-low-invoice.json"
    low_invoice_ref_registry.write_text(
        json.dumps(
            {
                "model_path": "data/models/spacy_ner/fake/model-best",
                "trained_at": "2026-03-05T14:00:00Z",
                "overall_f1": 0.90,
                "per_label_f1": {"INVOICE_REF": 0.89},
                "labels": ["INVOICE_REF"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="INVOICE_REF"):
        resolve_enabled_ner_model(
            enabled=True,
            registry_path=low_invoice_ref_registry,
            min_overall_f1=0.60,
            min_invoice_ref_f1=0.90,
        )


def test_pipeline_persists_ner_provenance_when_enabled(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-ner-provenance.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    with testing_session_local() as session:
        _seed_taxonomy(session)

    model_dir = tmp_path / "fake-spacy-model"
    model_dir.mkdir(parents=True, exist_ok=True)
    registry_path = tmp_path / "latest.json"
    registry_payload = {
        "model_path": str(model_dir),
        "trained_at": "2026-03-05T14:10:00Z",
        "overall_f1": 0.95,
        "per_label_f1": {"INVOICE_REF": 0.99},
        "labels": ["INVOICE_REF"],
    }
    registry_path.write_text(json.dumps(registry_payload), encoding="utf-8")

    monkeypatch.setattr(settings, "ner_enabled", True)
    monkeypatch.setattr(settings, "ner_registry_path", str(registry_path))
    monkeypatch.setattr(settings, "ner_min_overall_f1", 0.60)
    monkeypatch.setattr(settings, "ner_min_invoice_ref_f1", 0.90)
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
            "Invoice Ref INV-1001 Date 2026-03-05 Material Paper or cardboard 1.0 kg"
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
                "conf": ["95", "94"],
                "text": ["INV-1001", "Paper"],
            }
            if output_type is not None
            else (
                "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\t"
                "width\\theight\\tconf\\ttext\\n"
                "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t20\\t10\\t95\\tINV-1001\\n"
                "5\\t1\\t1\\t1\\t1\\t2\\t40\\t10\\t20\\t10\\t94\\tPaper\\n"
            )
        ),
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_pdf_or_hocr",
        lambda image, extension, config: b"<html>hocr</html>",
    )

    class FakeSpacyNERExtractor:
        def __init__(self, *, model_path: str) -> None:
            self.model_path = model_path

        def extract(self, text: str):
            return []

    monkeypatch.setattr(ner_spacy_service, "SpacyNERExtractor", FakeSpacyNERExtractor)

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class FakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", FakeJobQueue)

    client = TestClient(app)
    auth_headers = {"X-User-Id": "ner-user", "X-Tenant-Id": "1"}

    presign = client.post(
        "/api/v1/documents/upload/presign",
        headers=auth_headers,
        json={
            "filename": "ner-proof.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(b"%PDF-1.4\n%stub"),
        },
    )
    assert presign.status_code == 200
    presigned = presign.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=b"%PDF-1.4\n%stub",
        content_type="application/pdf",
    )

    finalise = client.post(
        "/api/v1/documents/upload/finalise",
        headers=auth_headers,
        json={"upload_id": presigned["upload_id"]},
    )
    assert finalise.status_code == 200
    document_id = finalise.json()["document_id"]
    assert len(enqueued) == 1

    run_response = client.post(f"/api/v1/pipeline/run/{document_id}", headers=auth_headers)
    assert run_response.status_code == 200
    assert run_response.json()["status"] == "COMPLETE"

    with testing_session_local() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.ner_model_path == str(model_dir)
        assert document.ner_model_f1 is not None
        assert float(document.ner_model_f1) == pytest.approx(0.95)
        assert document.ner_model_trained_at is not None
        trained_at_iso = document.ner_model_trained_at.replace(tzinfo=timezone.utc).isoformat()
        assert trained_at_iso.startswith("2026-03-05T14:10:00")

        job = (
            session.execute(select(Job).where(Job.document_id == UUID(document_id)))
            .scalars()
            .first()
        )
        assert job is not None
        assert job.ner_model_path == str(model_dir)
        assert float(job.ner_model_f1) == pytest.approx(0.95)

        ner_events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "document",
                    AuditEvent.entity_id == document_id,
                    AuditEvent.event_type == "NER_MODEL_USED",
                )
            )
            .scalars()
            .all()
        )
        assert len(ner_events) >= 1
        payload = ner_events[-1].payload
        assert payload["model_path"] == str(model_dir)
        assert payload["overall_f1"] == pytest.approx(0.95)
        assert payload["per_label_f1"]["INVOICE_REF"] == pytest.approx(0.99)


def test_tenant_toggle_disables_ner_even_with_global_enabled(tmp_path, monkeypatch) -> None:
    sqlite_path = tmp_path / "packtrack-tenant-ner-toggle.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    with testing_session_local() as session:
        _seed_taxonomy(session)

    model_dir = tmp_path / "fake-spacy-model"
    model_dir.mkdir(parents=True, exist_ok=True)
    registry_path = tmp_path / "latest.json"
    registry_path.write_text(
        json.dumps(
            {
                "model_path": str(model_dir),
                "trained_at": "2026-03-05T14:10:00Z",
                "overall_f1": 0.95,
                "per_label_f1": {"INVOICE_REF": 0.99},
                "labels": ["INVOICE_REF"],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "ner_enabled", True)
    monkeypatch.setattr(settings, "ner_registry_path", str(registry_path))
    monkeypatch.setattr(settings, "ner_min_overall_f1", 0.60)
    monkeypatch.setattr(settings, "ner_min_invoice_ref_f1", 0.90)
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
            "Invoice Ref INV-1001 Date 2026-03-05 Material Paper or cardboard 1.0 kg"
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
                "conf": ["95", "94"],
                "text": ["INV-1001", "Paper"],
            }
            if output_type is not None
            else (
                "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\t"
                "width\\theight\\tconf\\ttext\\n"
                "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t20\\t10\\t95\\tINV-1001\\n"
                "5\\t1\\t1\\t1\\t1\\t2\\t40\\t10\\t20\\t10\\t94\\tPaper\\n"
            )
        ),
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_pdf_or_hocr",
        lambda image, extension, config: b"<html>hocr</html>",
    )

    class ExplodingSpacyNERExtractor:
        def __init__(self, *, model_path: str) -> None:
            raise AssertionError("SpacyNERExtractor must not be instantiated when tenant disabled")

        def extract(self, text: str):
            return []

    monkeypatch.setattr(ner_spacy_service, "SpacyNERExtractor", ExplodingSpacyNERExtractor)

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class FakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", FakeJobQueue)

    client = TestClient(app)
    tenant_headers = {"X-User-Id": "ner-user", "X-Tenant-Id": "1"}
    admin_headers = {
        "X-User-Id": "ops-admin",
        "X-Tenant-Id": "1",
        "X-User-Role": "admin",
    }

    toggle = client.patch(
        "/api/v1/admin/tenants/1/settings",
        headers=admin_headers,
        json={"ner_enabled": False},
    )
    assert toggle.status_code == 200
    assert toggle.json() == {"tenant_id": 1, "ner_enabled": False}

    presign = client.post(
        "/api/v1/documents/upload/presign",
        headers=tenant_headers,
        json={
            "filename": "ner-disabled-proof.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(b"%PDF-1.4\n%stub"),
        },
    )
    assert presign.status_code == 200
    presigned = presign.json()

    storage = ObjectStorage()
    storage.put_bytes(
        bucket=presigned["bucket"],
        key=presigned["object_key"],
        data=b"%PDF-1.4\n%stub",
        content_type="application/pdf",
    )

    finalise = client.post(
        "/api/v1/documents/upload/finalise",
        headers=tenant_headers,
        json={"upload_id": presigned["upload_id"]},
    )
    assert finalise.status_code == 200
    document_id = finalise.json()["document_id"]
    assert len(enqueued) == 1

    run_response = client.post(f"/api/v1/pipeline/run/{document_id}", headers=tenant_headers)
    assert run_response.status_code == 200
    assert run_response.json()["status"] == "COMPLETE"

    with testing_session_local() as session:
        document = session.get(Document, UUID(document_id))
        assert document is not None
        assert document.ner_model_path is None
        assert document.ner_model_f1 is None
        assert document.ner_model_trained_at is None

        ner_used_events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "document",
                    AuditEvent.entity_id == document_id,
                    AuditEvent.event_type == "NER_MODEL_USED",
                )
            )
            .scalars()
            .all()
        )
        assert ner_used_events == []

        tenant_settings_events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "tenant",
                    AuditEvent.entity_id == "1",
                    AuditEvent.event_type == "TENANT_SETTINGS_UPDATED",
                )
            )
            .scalars()
            .all()
        )
        assert len(tenant_settings_events) >= 1
        payload = tenant_settings_events[-1].payload
        assert payload["previous_ner_enabled"] is True
        assert payload["ner_enabled"] is False
        assert payload["changed"] is True

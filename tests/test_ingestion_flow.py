from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import api.app.db.session as db_session_module
from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import Document, Job, UploadSession
from api.app.main import app
from api.app.routers import documents as documents_router
from api.app.services.storage import ObjectStorage


def test_presign_and_finalise_create_document_job_and_enqueue(tmp_path, monkeypatch) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'ingest.db'}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)
    monkeypatch.setattr(settings, "processing_queue_name", "packtrack:queue:preprocess")

    enqueued: list[tuple[str, dict[str, str | int]]] = []

    class FakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            enqueued.append((queue_name, payload))

    monkeypatch.setattr(documents_router, "JobQueue", FakeJobQueue)

    client = TestClient(app)
    headers = {"X-User-Id": "alice", "X-Tenant-Id": "123456"}
    file_bytes = b"%PDF-1.4\n%ingestion"

    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers=headers,
        json={
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(file_bytes),
        },
    )
    assert presign_response.status_code == 200
    presign_payload = presign_response.json()

    ObjectStorage().put_bytes(
        bucket=presign_payload["bucket"],
        key=presign_payload["object_key"],
        data=file_bytes,
        content_type="application/pdf",
    )

    finalise_response = client.post(
        "/api/v1/documents/upload/finalise",
        headers=headers,
        json={"upload_id": presign_payload["upload_id"]},
    )
    assert finalise_response.status_code == 200

    finalised = finalise_response.json()
    assert finalised["status"] == "QUEUED"
    assert len(enqueued) == 1
    assert enqueued[0][0] == "packtrack:queue:preprocess"
    assert enqueued[0][1]["job_id"] == finalised["job_id"]
    assert enqueued[0][1]["document_id"] == finalised["document_id"]

    with testing_session_local() as session:
        upload_session = session.get(UploadSession, UUID(presign_payload["upload_id"]))
        assert upload_session is not None
        assert upload_session.status == "FINALISED"
        assert upload_session.tenant_id == 123456

        document = session.get(Document, UUID(finalised["document_id"]))
        assert document is not None
        assert document.organisation_id == 123456
        assert document.mime_type == "application/pdf"
        assert document.file_size_bytes == len(file_bytes)
        assert document.checksum_sha256 == hashlib.sha256(file_bytes).hexdigest()
        assert document.uploaded_by == "alice"
        assert document.storage_path.startswith("minio://raw-uploads/")

        job = session.get(Job, UUID(finalised["job_id"]))
        assert job is not None
        assert job.document_id == document.id
        assert job.organisation_id == 123456
        assert job.status == "QUEUED"
        assert job.current_stage == "QUEUED"
        assert job.queue_name == "packtrack:queue:preprocess"


def test_upload_endpoints_enforce_auth_and_tenant_isolation(tmp_path, monkeypatch) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'auth.db'}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(db_session_module, "engine", engine)
    monkeypatch.setattr(db_session_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)

    class FakeJobQueue:
        def enqueue(self, queue_name: str, payload: dict[str, str | int]) -> None:
            return None

    monkeypatch.setattr(documents_router, "JobQueue", FakeJobQueue)

    client = TestClient(app)

    unauth_presign = client.post(
        "/api/v1/documents/upload/presign",
        json={
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 10,
        },
    )
    assert unauth_presign.status_code == 401

    presign_response = client.post(
        "/api/v1/documents/upload/presign",
        headers={"X-User-Id": "alice", "X-Tenant-Id": "123456"},
        json={
            "filename": "invoice.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 10,
        },
    )
    assert presign_response.status_code == 200
    payload = presign_response.json()

    ObjectStorage().put_bytes(
        bucket=payload["bucket"],
        key=payload["object_key"],
        data=b"0123456789",
        content_type="application/pdf",
    )

    wrong_tenant_finalise = client.post(
        "/api/v1/documents/upload/finalise",
        headers={"X-User-Id": "bob", "X-Tenant-Id": "999999"},
        json={"upload_id": payload["upload_id"]},
    )
    assert wrong_tenant_finalise.status_code == 404

    with testing_session_local() as session:
        jobs = session.execute(select(Job)).scalars().all()
    assert jobs == []

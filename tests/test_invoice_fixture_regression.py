from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import textwrap
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import (
    Classification,
    Document,
    ExtractedEntity,
    Job,
    Page,
    Report,
    TaxonomyCode,
)
from api.app.schemas.defra import DEFRA_REPORT_COLUMNS
from api.app.services import ocr as ocr_service
from api.app.services.pipeline_runner import PipelineRunner
from api.app.services.storage import ObjectStorage
from api.app.services.taxonomy_loader import load_taxonomy_from_excel

pytestmark = pytest.mark.slow


def _resolve_workbook_path() -> Path:
    candidates = [
        Path("data/defra/UK_DEFRA.xlsx"),
        Path("data/defra/UK DEFRA.xlsx"),
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("DEFRA workbook not found")


def _load_fixture_index() -> list[dict]:
    index_path = Path("tests/fixtures/invoices/index.json")
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    return payload["fixtures"]


def _assert_ocr_contains(*, ocr_text: str, expected: str, label: str) -> None:
    preview = ocr_text[:500]
    assert expected in ocr_text, (
        f"OCR output missing {label} '{expected}'. "
        f"First 500 chars: {preview!r}"
    )


def _configure_tesseract(monkeypatch, tmp_path: Path) -> None:
    local_tesseract = shutil.which("tesseract")
    if local_tesseract:
        monkeypatch.setattr(ocr_service.pytesseract.pytesseract, "tesseract_cmd", local_tesseract)
        return

    docker_bin = shutil.which("docker")
    if not docker_bin:
        raise RuntimeError("tesseract is not installed and docker fallback is unavailable")

    container_name = os.environ.get("PACKTRACK_TESSERACT_CONTAINER", "packtrack-api")
    inspect = subprocess.run(
        [docker_bin, "inspect", "-f", "{{.State.Running}}", container_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspect.returncode != 0 or inspect.stdout.strip() != "true":
        raise RuntimeError(
            "tesseract is not installed locally and fallback container "
            f"'{container_name}' is not running"
        )

    shim_path = tmp_path / "docker_tesseract_shim.py"
    shim_code = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import subprocess
        import sys
        import uuid

        CONTAINER = {container_name!r}
        DOCKER = {docker_bin!r}

        def _detect_extension(args):
            known = {"txt", "tsv", "hocr", "pdf", "box", "osd", "xml", "alto"}
            for token in reversed(args):
                if token in known:
                    return token
            for idx, token in enumerate(args):
                if token != "-c" or idx + 1 >= len(args):
                    continue
                config_arg = args[idx + 1].lower()
                if "tessedit_create_tsv=1" in config_arg:
                    return "tsv"
                if "tessedit_create_hocr=1" in config_arg:
                    return "hocr"
                if "tessedit_create_pdf=1" in config_arg:
                    return "pdf"
            return "txt"

        def _run():
            if len(sys.argv) == 2 and sys.argv[1].startswith("-"):
                process = subprocess.run(
                    [DOCKER, "exec", CONTAINER, "tesseract", sys.argv[1]],
                    check=False,
                )
                return process.returncode
            if len(sys.argv) < 3:
                return 1
            source = sys.argv[1]
            output_base = sys.argv[2]
            passthrough = sys.argv[3:]
            ext = _detect_extension(passthrough)

            suffix = uuid.uuid4().hex
            remote_source = f"/tmp/{{suffix}}-source.png"
            remote_output_base = f"/tmp/{{suffix}}-out"
            remote_output = f"{{remote_output_base}}.{{ext}}"
            local_output = f"{{output_base}}.{{ext}}"

            try:
                subprocess.run(
                    [DOCKER, "cp", source, f"{{CONTAINER}}:{{remote_source}}"],
                    check=True,
                )
                process = subprocess.run(
                    [
                        DOCKER,
                        "exec",
                        CONTAINER,
                        "tesseract",
                        remote_source,
                        remote_output_base,
                        *passthrough,
                    ],
                    check=False,
                )
                if process.returncode != 0:
                    return process.returncode
                subprocess.run(
                    [DOCKER, "cp", f"{{CONTAINER}}:{{remote_output}}", local_output],
                    check=True,
                )
                return 0
            finally:
                subprocess.run(
                    [DOCKER, "exec", CONTAINER, "rm", "-f", remote_source, remote_output],
                    check=False,
                )

        raise SystemExit(_run())
        """
    )
    shim_path.write_text(shim_code, encoding="utf-8")
    shim_path.chmod(0o755)
    monkeypatch.setattr(ocr_service.pytesseract.pytesseract, "tesseract_cmd", str(shim_path))


FIXTURES = _load_fixture_index()


@pytest.mark.parametrize("fixture", FIXTURES, ids=[item["pdf_file"] for item in FIXTURES])
def test_invoice_pdf_fixtures_regression_pipeline(tmp_path, monkeypatch, fixture) -> None:
    _configure_tesseract(monkeypatch, tmp_path)

    sqlite_path = tmp_path / "invoice-fixtures.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{sqlite_path}",
        connect_args={"check_same_thread": False},
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))

    with testing_session_local() as session:
        load_taxonomy_from_excel(session=session, excel_path=_resolve_workbook_path())
        session.commit()

    storage = ObjectStorage()

    fixture_dir = Path("tests/fixtures/invoices")
    image_name = fixture.get("fallback_image_file") or fixture["pdf_file"].replace(".pdf", ".png")
    image_path = fixture_dir / image_name
    image_bytes = image_path.read_bytes()

    document_id = uuid4()
    job_id = uuid4()
    raw_uri = storage.put_bytes(
        bucket=settings.minio_bucket_raw,
        key=f"raw-uploads/123456/{document_id}/{image_name}",
        data=image_bytes,
        content_type="image/png",
    )

    with testing_session_local() as session:
        document = Document(
            id=document_id,
            organisation_id=123456,
            subsidiary_id="",
            organisation_size="L",
            submission_period="2025-P1",
            original_filename=image_name,
            mime_type="image/png",
            file_size_bytes=len(image_bytes),
            checksum_sha256=None,
            uploaded_by="fixture-test",
            storage_path=raw_uri,
            status="QUEUED",
        )
        job = Job(
            id=job_id,
            document_id=document_id,
            organisation_id=123456,
            status="QUEUED",
            current_stage="QUEUED",
            queue_name=settings.processing_queue_name,
            attempt_count=0,
            error_message=None,
        )
        session.add(document)
        session.add(job)
        session.commit()

    with testing_session_local() as session:
        result = PipelineRunner(session=session, storage=ObjectStorage()).run(
            document_id=document_id
        )
        session.commit()

        assert result.status == "COMPLETE"

        pages = (
            session.execute(
                select(Page).where(Page.document_id == document_id).order_by(Page.page_number)
            )
            .scalars()
            .all()
        )
        assert pages

        page_text = "\n".join((page.ocr_text or "") for page in pages)
        expected_ref = fixture["expected_extracted_fields"]["invoice_ref"]
        supplier_token = fixture["expected_extracted_fields"].get("stable_ocr_token") or (
            fixture["expected_extracted_fields"]["supplier_name"].split()[0]
        )
        _assert_ocr_contains(ocr_text=page_text, expected=expected_ref, label="invoice_ref")
        _assert_ocr_contains(ocr_text=page_text, expected=supplier_token, label="supplier token")

        for page in pages:
            text_key = f"ocr-output/{document_id}/page-{page.page_number}.txt"
            tsv_key = f"ocr-output/{document_id}/page-{page.page_number}.tsv"
            hocr_key = f"ocr-output/{document_id}/page-{page.page_number}.hocr"
            stored_text = storage.get_bytes(
                bucket=settings.minio_bucket_raw,
                key=text_key,
            ).decode("utf-8", errors="replace")
            storage.get_bytes(bucket=settings.minio_bucket_raw, key=tsv_key)
            storage.get_bytes(bucket=settings.minio_bucket_raw, key=hocr_key)
            _assert_ocr_contains(
                ocr_text=stored_text,
                expected=expected_ref,
                label=f"invoice_ref in {text_key}",
            )

        extracted = (
            session.execute(
                select(ExtractedEntity).where(ExtractedEntity.document_id == document_id)
            )
            .scalars()
            .all()
        )
        extracted_fields = {row.field_name for row in extracted}
        required_fields = fixture["expected_extracted_fields"]["required_fields"]
        for field_name in required_fields:
            if field_name == "supplier_name":
                continue
            assert field_name in extracted_fields

        classification = (
            session.execute(select(Classification).where(Classification.document_id == document_id))
            .scalars()
            .first()
        )
        assert classification is not None
        assert classification.taxonomy_code in fixture["expected_taxonomy_codes"]

        taxonomy_row = (
            session.execute(
                select(TaxonomyCode).where(
                    TaxonomyCode.active.is_(True),
                    TaxonomyCode.code == classification.taxonomy_code,
                )
            )
            .scalars()
            .first()
        )
        assert taxonomy_row is not None

        report = session.get(Report, UUID(result.report_id))
        assert report is not None
        assert report.output_path is not None

        bucket, key = ObjectStorage.parse_uri(report.output_path)
        csv_bytes = storage.get_bytes(bucket=bucket, key=key)
        header = next(csv.reader(StringIO(csv_bytes.decode("utf-8"))))
        assert header == DEFRA_REPORT_COLUMNS

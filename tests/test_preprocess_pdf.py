from __future__ import annotations

from uuid import uuid4

from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import AuditEvent, Document, Page, TaxonomyCode
from api.app.services import ocr as ocr_service
from api.app.services import preprocess as preprocess_service
from api.app.services.pipeline_runner import PipelineRunner
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


def test_pdf_preprocess_creates_pages_and_minio_objects(tmp_path, monkeypatch) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'preprocess.db'}",
        connect_args={"check_same_thread": False},
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)
    with testing_session_local() as session:
        _seed_taxonomy(session)

    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))
    monkeypatch.setattr(
        preprocess_service,
        "convert_from_bytes",
        lambda payload, dpi, fmt: [
            Image.new("RGB", (800, 600), color="white"),
            Image.new("RGB", (1024, 768), color="white"),
        ],
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: "sample OCR text",
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_data",
        lambda image, config, output_type=None: (
            {
                "level": [5],
                "page_num": [1],
                "block_num": [1],
                "par_num": [1],
                "line_num": [1],
                "word_num": [1],
                "left": [10],
                "top": [10],
                "width": [30],
                "height": [10],
                "conf": ["95"],
                "text": ["sample"],
            }
            if output_type is not None
            else (
                "level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\t"
                "width\\theight\\tconf\\ttext\\n"
                "5\\t1\\t1\\t1\\t1\\t1\\t10\\t10\\t30\\t10\\t95\\tsample\\n"
            )
        ),
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_pdf_or_hocr",
        lambda image, extension, config: b"<html>hocr</html>",
    )

    storage = ObjectStorage()
    document_id = uuid4()
    raw_uri = storage.put_bytes(
        bucket=settings.minio_bucket_raw,
        key=f"raw-uploads/{document_id}/sample.pdf",
        data=b"%PDF-1.4\n%mock",
        content_type="application/pdf",
    )

    with testing_session_local() as session:
        document = Document(
            id=document_id,
            organisation_id=123456,
            subsidiary_id="",
            organisation_size="L",
            submission_period="2025-P1",
            original_filename="sample.pdf",
            mime_type="application/pdf",
            file_size_bytes=13,
            checksum_sha256=None,
            uploaded_by="tester",
            storage_path=raw_uri,
            status="QUEUED",
        )
        session.add(document)
        session.commit()

    with testing_session_local() as session:
        runner = PipelineRunner(session=session, storage=ObjectStorage())
        result = runner.run(document_id=document_id)
        session.commit()

    assert result.status == "COMPLETE"

    with testing_session_local() as session:
        pages = (
            session.execute(
                select(Page).where(Page.document_id == document_id).order_by(Page.page_number.asc())
            )
            .scalars()
            .all()
        )
        assert len(pages) == 2

        for page in pages:
            assert page.page_width and page.page_width > 0
            assert page.page_height and page.page_height > 0
            assert page.raw_image_path
            assert page.normalised_image_path
            assert page.image_path == page.normalised_image_path
            assert page.processing_time_ms is not None

            raw_bucket, raw_key = ObjectStorage.parse_uri(page.raw_image_path)
            normalised_bucket, normalised_key = ObjectStorage.parse_uri(page.normalised_image_path)
            assert storage.get_bytes(raw_bucket, raw_key)
            assert storage.get_bytes(normalised_bucket, normalised_key)

        events = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "document",
                    AuditEvent.entity_id == str(document_id),
                )
            )
            .scalars()
            .all()
        )

    event_types = {event.event_type for event in events}
    assert "PREPROCESS_PAGE_PROCESSED" in event_types
    assert "PREPROCESS_STAGE_FINISHED" in event_types

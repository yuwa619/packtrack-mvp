from __future__ import annotations

from uuid import uuid4

from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import AuditEvent, Document, Entity, Page, ReviewTask, TaxonomyCode
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


def test_ocr_outputs_persist_and_low_confidence_creates_review_tasks(tmp_path, monkeypatch) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'ocr.db'}", connect_args={"check_same_thread": False}
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)
    with testing_session_local() as session:
        _seed_taxonomy(session)

    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))
    monkeypatch.setattr(settings, "ocr_confidence_threshold", 0.70)

    monkeypatch.setattr(
        preprocess_service,
        "convert_from_bytes",
        lambda payload, dpi, fmt: [Image.new("RGB", (640, 480), color="white")],
    )
    monkeypatch.setattr(
        ocr_service.pytesseract,
        "image_to_string",
        lambda image, config: "lowtoken hightoken",
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
                "left": [10, 50],
                "top": [10, 10],
                "width": [30, 40],
                "height": [10, 10],
                "conf": ["45", "92"],
                "text": ["lowtoken", "hightoken"],
            }
            if output_type is not None
            else (
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\t"
                "width\theight\tconf\ttext\n"
                "5\t1\t1\t1\t1\t1\t10\t10\t30\t10\t45\tlowtoken\n"
                "5\t1\t1\t1\t1\t2\t50\t10\t40\t10\t92\thightoken\n"
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
    source_uri = storage.put_bytes(
        bucket=settings.minio_bucket_raw,
        key=f"raw-uploads/{document_id}/ocr-source.pdf",
        data=b"%PDF-1.4\n%ocr",
        content_type="application/pdf",
    )

    with testing_session_local() as session:
        document = Document(
            id=document_id,
            organisation_id=123456,
            subsidiary_id="",
            organisation_size="L",
            submission_period="2025-P1",
            original_filename="ocr-source.pdf",
            mime_type="application/pdf",
            file_size_bytes=13,
            checksum_sha256=None,
            uploaded_by="tester",
            storage_path=source_uri,
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
        page = (
            session.execute(select(Page).where(Page.document_id == document_id).limit(1))
            .scalars()
            .first()
        )
        assert page is not None
        assert page.ocr_text == "lowtoken hightoken"

        entities = (
            session.execute(
                select(Entity).where(Entity.page_id == page.id).order_by(Entity.label.asc())
            )
            .scalars()
            .all()
        )
        labels = {entity.label for entity in entities}
        assert "OCR_BLOCK" in labels
        assert "OCR_LINE" in labels
        assert "OCR_TOKEN" in labels

        ocr_review_tasks = (
            session.execute(
                select(ReviewTask).where(
                    ReviewTask.document_id == document_id,
                    ReviewTask.task_type == "OCR_REVIEW",
                )
            )
            .scalars()
            .all()
        )
        assert len(ocr_review_tasks) == 1
        notes = ocr_review_tasks[0].notes or ""
        assert "low_conf_token_count" in notes
        assert "examples" in notes
        assert "avg_confidence" in notes
        assert "ocr_artifact_uri" in notes

        ocr_page_event = (
            session.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "document",
                    AuditEvent.entity_id == str(document_id),
                    AuditEvent.event_type == "OCR_PAGE_PROCESSED",
                )
            )
            .scalars()
            .first()
        )

    assert ocr_page_event is not None
    payload = ocr_page_event.payload
    for key in ("text_uri", "tsv_uri", "hocr_uri"):
        bucket, object_key = ObjectStorage.parse_uri(payload[key])
        stored = storage.get_bytes(bucket, object_key)
        assert stored

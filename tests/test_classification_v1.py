from __future__ import annotations

from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from api.app.config import settings
from api.app.db.base import Base
from api.app.db.models import (
    Classification,
    Document,
    ExtractedEntity,
    Page,
    ReviewTask,
    TaxonomyCode,
)
from api.app.services.classification_v1 import ClassificationServiceV1
from api.app.services.pipeline_runner import PipelineRunner
from api.app.services.storage import ObjectStorage


def _seed_taxonomy(session: Session) -> None:
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


def test_invalid_rule_codes_are_rejected_by_taxonomy_validation(tmp_path, monkeypatch) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'classification.db'}",
        connect_args={"check_same_thread": False},
    )
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    with session_local() as session:
        _seed_taxonomy(session)

        document_id = uuid4()
        session.add(
            Document(
                id=document_id,
                organisation_id=123456,
                subsidiary_id="",
                organisation_size="L",
                submission_period="2025-P1",
                original_filename="invoice.pdf",
                mime_type="application/pdf",
                file_size_bytes=100,
                checksum_sha256=None,
                uploaded_by="tester",
                storage_path="minio://raw-uploads/sample",
                status="CLASSIFYING",
            )
        )
        session.add(
            Page(
                document_id=document_id,
                page_number=1,
                ocr_text="bioplastic packaging household primary brand",
                image_path="minio://preprocessed/sample",
            )
        )
        session.add(
            ExtractedEntity(
                document_id=document_id,
                page_id=None,
                field_name="product_desc",
                raw_value="bioplastic tray",
                normalized_value="bioplastic tray",
                confidence=0.9,
                source_page_number=1,
                source_block_number=1,
                source_line_number=1,
                start_offset=0,
                end_offset=15,
                provenance={"method": "test"},
            )
        )
        session.commit()

        decision = ClassificationServiceV1(session=session).classify_document(
            document_id=document_id
        )

        assert decision.packaging_material in {"Plastic", "Paper or cardboard", "Glass", "Wood"}
        assert all(candidate["code"] != "BIO" for candidate in decision.candidates)


def test_ambiguous_case_produces_top3_and_creates_review_task(tmp_path, monkeypatch) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'classification_ambiguous.db'}",
        connect_args={"check_same_thread": False},
    )
    session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    monkeypatch.setattr(settings, "minio_force_local", True)
    monkeypatch.setattr(settings, "minio_allow_local_fallback", True)
    monkeypatch.setattr(settings, "minio_fallback_dir", str(tmp_path / "minio"))

    document_id = uuid4()

    with session_local() as session:
        _seed_taxonomy(session)
        session.add(
            Document(
                id=document_id,
                organisation_id=123456,
                subsidiary_id="",
                organisation_size="L",
                submission_period="2025-P1",
                original_filename="invoice.pdf",
                mime_type="application/pdf",
                file_size_bytes=100,
                checksum_sha256=None,
                uploaded_by="tester",
                storage_path="minio://raw-uploads/sample",
                status="CLASSIFYING",
            )
        )
        session.add(
            Page(
                document_id=document_id,
                page_number=1,
                ocr_text=(
                    "household primary brand material includes plastic glass paper "
                    "and wood for mixed packaging"
                ),
                image_path="minio://preprocessed/sample",
            )
        )
        session.commit()

        runner = PipelineRunner(session=session, storage=ObjectStorage())
        document = session.get(Document, document_id)
        assert document is not None
        result = runner._run_classify(document)
        session.commit()

    assert result["confidence"] < 0.85
    assert len(result["candidates"]) == 3

    with session_local() as session:
        classification = (
            session.execute(select(Classification).where(Classification.document_id == document_id))
            .scalars()
            .first()
        )
        assert classification is not None
        assert classification.candidate_codes is not None
        assert len(classification.candidate_codes) == 3
        assert classification.taxonomy_version == "taxonomy for the UK DEFRA Exten"

        review_task = (
            session.execute(
                select(ReviewTask).where(
                    ReviewTask.document_id == document_id,
                    ReviewTask.task_type == "CLASSIFICATION_REVIEW",
                )
            )
            .scalars()
            .first()
        )
        assert review_task is not None

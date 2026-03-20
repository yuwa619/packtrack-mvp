from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.app.db.base import Base
from api.app.db.models import Document, TrainingSample
from api.scripts import export_training_samples_jsonl as exporter


def test_export_normalises_invalid_spans(tmp_path, monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False})
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    @contextmanager
    def _test_db_session():
        session = testing_session_local()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(exporter, "db_session", _test_db_session)

    ocr_text = "Supplier Acme\\nInvoice Ref INV-1001\\nInvoice Date 2025-01-15\\n"
    document_id = uuid4()
    with _test_db_session() as session:
        session.add(
            Document(
                id=document_id,
                organisation_id=1,
                original_filename="sample.pdf",
                mime_type="application/pdf",
                storage_path="minio://raw/sample.pdf",
                status="COMPLETE",
            )
        )
        session.add(
            TrainingSample(
                document_id=document_id,
                page_number=1,
                ocr_text=ocr_text,
                span_start=0,
                span_end=8,
                corrected_value="INV-1001",
                field_name="invoice_ref",
                source="field_correction",
                taxonomy_code=None,
                reviewer="qa-user",
            )
        )
        session.add(
            TrainingSample(
                document_id=document_id,
                page_number=1,
                ocr_text=ocr_text,
                span_start=0,
                span_end=8,
                corrected_value="NOT-IN-TEXT",
                field_name="supplier_ref",
                source="field_correction",
                taxonomy_code=None,
                reviewer="qa-user",
            )
        )

    output_path = tmp_path / "training_samples.jsonl"
    exported_count = exporter.export_training_samples_jsonl(output_path=output_path)
    assert exported_count == 2

    rows = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(rows) == 2

    corrected_span = rows[0]["meta"]
    assert corrected_span["span_start"] is not None
    assert corrected_span["span_end"] is not None
    assert (
        ocr_text[corrected_span["span_start"] : corrected_span["span_end"]].casefold()
        == "inv-1001".casefold()
    )

    nulled_span = rows[1]["meta"]
    assert nulled_span["span_start"] is None
    assert nulled_span["span_end"] is None

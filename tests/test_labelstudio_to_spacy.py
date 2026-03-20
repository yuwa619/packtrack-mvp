from __future__ import annotations

import json
from pathlib import Path

import pytest

spacy = pytest.importorskip("spacy", reason="spacy is an optional ML training dependency")
from spacy.tokens import DocBin  # noqa: E402

from api.scripts.labelstudio_to_spacy import convert_labelstudio_export  # noqa: E402


def test_labelstudio_converter_creates_docbins_and_labels(tmp_path: Path) -> None:
    fixture_path = Path("tests/fixtures/labelstudio/minimal_export.json")
    output_dir = tmp_path / "spacy"

    summary = convert_labelstudio_export(export_path=fixture_path, output_dir=output_dir, seed=7)

    train_path = output_dir / "train.spacy"
    dev_path = output_dir / "dev.spacy"
    labels_path = output_dir / "labels.json"
    assert train_path.exists()
    assert dev_path.exists()
    assert labels_path.exists()

    labels_payload = json.loads(labels_path.read_text(encoding="utf-8"))
    labels = set(labels_payload["labels"])
    assert "INVOICE_REF" in labels
    assert "SUPPLIER_NAME" in labels
    assert "WEIGHT_VALUE" in labels
    assert labels_payload["label_counts"]["INVOICE_REF"] >= 1

    # The fixture contains intentionally wrong spans; converter should repair via search.
    assert summary["kept_spans"] >= 5
    assert summary["repaired_spans"] >= 1

    nlp = spacy.blank("en")
    train_docs = list(DocBin().from_disk(train_path).get_docs(nlp.vocab))
    dev_docs = list(DocBin().from_disk(dev_path).get_docs(nlp.vocab))
    assert train_docs
    assert train_docs or dev_docs

    all_ents = [ent.label_ for doc in train_docs + dev_docs for ent in doc.ents]
    assert "INVOICE_REF" in all_ents

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("spacy", reason="spacy is required for NER A/B comparison")

from tests.fixtures.invoices.evaluate_fixtures import evaluate_fixtures  # noqa: E402

pytestmark = pytest.mark.slow


@pytest.mark.timeout(360)
def test_fixture_ab_ner_not_worse_than_heuristics(tmp_path: Path) -> None:
    output_dir = tmp_path / "fixture_eval_ab"
    results = evaluate_fixtures(output_dir=output_dir, mode="both")

    heuristics = results["heuristics"]["overall"]["extraction_coverage_pct"]
    ner = results["ner"]["overall"]["extraction_coverage_pct"]
    assert ner >= heuristics

    assert (output_dir / "metrics_heuristics.json").exists()
    assert (output_dir / "metrics_ner.json").exists()

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.invoices.evaluate_fixtures import evaluate_fixtures

pytestmark = pytest.mark.slow


@pytest.mark.timeout(180)
def test_fixture_metrics_thresholds(tmp_path: Path) -> None:
    output_dir = tmp_path / "fixture_eval"
    metrics = evaluate_fixtures(output_dir=output_dir)
    overall = metrics["overall"]

    assert overall["ocr_pass_rate_pct"] == pytest.approx(100.0, abs=1e-6)
    assert overall["extraction_coverage_pct"] >= 90.0
    assert overall["classification_match_rate_pct"] >= 75.0
    assert overall["avg_runtime_sec"] < 30.0

    assert (output_dir / "metrics.json").exists()
    assert (output_dir / "metrics.txt").exists()

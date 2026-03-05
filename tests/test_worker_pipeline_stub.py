from __future__ import annotations

from uuid import uuid4

from worker.app.services.pipeline import run_mock_pipeline


def test_pipeline_stub_returns_mocked_stages() -> None:
    result = run_mock_pipeline(uuid4())

    assert result["status"] == "completed-mocked"
    stage_names = [stage["name"] for stage in result["stages"]]
    assert stage_names == ["ingest", "preprocess", "extract", "classify", "report"]

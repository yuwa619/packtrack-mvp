from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID

from fastapi.testclient import TestClient

from api.app.config import settings
from api.app.main import app
from api.app.routers import demo as demo_router
from api.app.services.pipeline_runner import PipelineRunResult


def test_demo_endpoints_return_ids_without_external_dependencies(monkeypatch) -> None:
    created_document_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    created_job_id = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    run_document_id = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    run_job_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    report_id = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    state = {"create_calls": 0}

    @contextmanager
    def _fake_db_session():
        yield object()

    def _fake_create_document_and_job(*, session, storage, auth):
        del session, storage, auth
        state["create_calls"] += 1
        if state["create_calls"] == 1:
            return created_document_id, created_job_id
        return run_document_id, run_job_id

    def _fake_run_pipeline(self, *, document_id):
        del self
        return PipelineRunResult(
            document_id=str(document_id),
            status="COMPLETE",
            report_id=str(report_id),
            review_task_count=0,
        )

    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(demo_router, "db_session", _fake_db_session)
    monkeypatch.setattr(demo_router, "_create_demo_document_and_job", _fake_create_document_and_job)
    monkeypatch.setattr(demo_router.PipelineRunner, "run", _fake_run_pipeline)

    client = TestClient(app)
    auth_headers = {"X-User-Id": "demo-user", "X-Tenant-Id": "123456"}

    create_response = client.post("/api/v1/demo/create-sample", headers=auth_headers)
    assert create_response.status_code == 200
    assert create_response.json() == {"document_id": str(created_document_id)}

    run_response = client.post("/api/v1/demo/run", headers=auth_headers)
    assert run_response.status_code == 200
    assert run_response.json() == {
        "document_id": str(run_document_id),
        "job_id": str(run_job_id),
        "report_id": str(report_id),
    }

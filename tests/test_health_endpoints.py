from __future__ import annotations

from fastapi.testclient import TestClient

from api.app.main import app as api_app
from worker.app.main import app as worker_app


def test_api_health_endpoint() -> None:
    client = TestClient(api_app)
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


def test_worker_health_endpoint() -> None:
    client = TestClient(worker_app)
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "worker"}

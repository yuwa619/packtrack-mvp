from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ..services.pipeline import run_mock_pipeline

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/run/{document_id}")
def run_pipeline(document_id: UUID) -> dict[str, object]:
    return run_mock_pipeline(document_id)

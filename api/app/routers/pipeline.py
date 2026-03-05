from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException

from ..db.models import Document
from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_auth_context
from ..services.idempotency import IdempotencyGuard
from ..services.pipeline_runner import PipelineRunner
from ..services.pipeline_state import InvalidTransitionError
from ..services.storage import ObjectStorage

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post("/run/{document_id}")
def run_pipeline(
    document_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, str | int]:
    with db_session() as session:
        document = session.get(Document, document_id)
        if document is None or document.organisation_id != auth.tenant_id:
            raise HTTPException(status_code=404, detail=f"Document {document_id} not found")

        idempotency = IdempotencyGuard(
            session=session,
            tenant_id=auth.tenant_id,
            scope="pipeline_run",
            idempotency_key=idempotency_key,
            request_payload={"document_id": str(document_id)},
        )
        replay = idempotency.begin()
        if replay is not None:
            return replay.payload

        runner = PipelineRunner(session=session, storage=ObjectStorage())
        try:
            result = runner.run(document_id=document.id)
        except InvalidTransitionError as exc:
            idempotency.failure(status_code=409, detail=str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            idempotency.failure(status_code=404, detail=str(exc))
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            idempotency.failure(status_code=500, detail=str(exc))
            raise HTTPException(status_code=500, detail=f"Pipeline failed: {exc}") from exc

        response = {
            "document_id": result.document_id,
            "status": result.status,
            "report_id": result.report_id,
            "review_task_count": result.review_task_count,
        }
        idempotency.success(response)
    return response

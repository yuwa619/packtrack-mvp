from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_admin_auth_context
from ..services.pilot_metrics import get_pilot_summary

router = APIRouter(prefix="/admin/metrics", tags=["admin"])


@router.get("/pilot-summary")
def pilot_summary(_admin: Annotated[AuthContext, Depends(get_admin_auth_context)]) -> dict:
    with db_session() as session:
        return get_pilot_summary(session=session, window_days=7)

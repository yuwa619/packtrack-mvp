from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..db.session import db_session
from ..dependencies.auth import AuthContext, get_admin_auth_context
from ..services.audit import add_audit_event
from ..services.tenant_settings import get_or_create_tenant_setting

router = APIRouter(prefix="/admin/tenants", tags=["admin"])


class TenantSettingsPatchRequest(BaseModel):
    ner_enabled: bool


@router.patch("/{tenant_id}/settings")
def patch_tenant_settings(
    tenant_id: int,
    request: TenantSettingsPatchRequest,
    admin: Annotated[AuthContext, Depends(get_admin_auth_context)],
) -> dict[str, int | bool]:
    if tenant_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid tenant_id")

    with db_session() as session:
        tenant_setting = get_or_create_tenant_setting(session=session, tenant_id=tenant_id)
        previous_ner_enabled = bool(tenant_setting.ner_enabled)
        tenant_setting.ner_enabled = bool(request.ner_enabled)
        session.add(tenant_setting)

        add_audit_event(
            session=session,
            event_type="TENANT_SETTINGS_UPDATED",
            entity_type="tenant",
            entity_id=str(tenant_id),
            payload={
                "updated_by": admin.user_id,
                "admin_tenant_id": admin.tenant_id,
                "previous_ner_enabled": previous_ner_enabled,
                "ner_enabled": tenant_setting.ner_enabled,
                "changed": previous_ner_enabled != tenant_setting.ner_enabled,
            },
        )

        return {
            "tenant_id": tenant_id,
            "ner_enabled": tenant_setting.ner_enabled,
        }

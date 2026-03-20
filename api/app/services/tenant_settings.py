from __future__ import annotations

from sqlalchemy.orm import Session

from ..config import settings
from ..db.models import TenantSetting


def default_ner_enabled_for_new_tenants() -> bool:
    return settings.environment in {"local", "pilot"}


def get_or_create_tenant_setting(*, session: Session, tenant_id: int) -> TenantSetting:
    tenant_setting = session.get(TenantSetting, tenant_id)
    if tenant_setting is not None:
        return tenant_setting

    tenant_setting = TenantSetting(
        tenant_id=tenant_id,
        ner_enabled=default_ner_enabled_for_new_tenants(),
    )
    session.add(tenant_setting)
    session.flush()
    return tenant_setting


def is_tenant_ner_enabled(*, session: Session, tenant_id: int) -> bool:
    return bool(get_or_create_tenant_setting(session=session, tenant_id=tenant_id).ner_enabled)

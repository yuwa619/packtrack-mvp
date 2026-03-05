from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException


@dataclass
class AuthContext:
    user_id: str
    tenant_id: int


def get_auth_context(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-Id"),
) -> AuthContext:
    if not x_user_id or x_tenant_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if x_tenant_id <= 0:
        raise HTTPException(status_code=403, detail="Invalid tenant context")
    return AuthContext(user_id=x_user_id, tenant_id=x_tenant_id)

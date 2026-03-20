from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException


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


def get_admin_auth_context(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
) -> AuthContext:
    if (x_user_role or "").strip().lower() != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return auth

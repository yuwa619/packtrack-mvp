from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import IdempotencyRecord


@dataclass
class IdempotencyReplay:
    status_code: int
    payload: dict[str, Any]


class IdempotencyGuard:
    def __init__(
        self,
        *,
        session: Session,
        tenant_id: int,
        scope: str,
        idempotency_key: str | None,
        request_payload: dict[str, Any],
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.scope = scope
        self.idempotency_key = idempotency_key.strip() if idempotency_key else None
        self.request_hash = self._hash_payload(request_payload)
        self._record: IdempotencyRecord | None = None

    @staticmethod
    def _hash_payload(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()

    def begin(self) -> IdempotencyReplay | None:
        if self.idempotency_key is None:
            return None
        if not self.idempotency_key:
            raise HTTPException(status_code=400, detail="Idempotency-Key cannot be empty")
        if len(self.idempotency_key) > 128:
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key cannot exceed 128 characters",
            )

        existing = (
            self.session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.tenant_id == self.tenant_id,
                    IdempotencyRecord.scope == self.scope,
                    IdempotencyRecord.idempotency_key == self.idempotency_key,
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            record = IdempotencyRecord(
                tenant_id=self.tenant_id,
                scope=self.scope,
                idempotency_key=self.idempotency_key,
                request_hash=self.request_hash,
                status="IN_PROGRESS",
                response_code=None,
                response_payload={},
            )
            self.session.add(record)
            self.session.flush()
            self._record = record
            return None

        if existing.request_hash != self.request_hash:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Idempotency-Key reuse detected with different request payload "
                    "for this tenant and operation"
                ),
            )
        if existing.status == "SUCCEEDED":
            return IdempotencyReplay(
                status_code=existing.response_code or 200,
                payload=existing.response_payload or {},
            )
        if existing.status == "FAILED":
            detail = (existing.response_payload or {}).get("detail", "Prior attempt failed")
            raise HTTPException(
                status_code=409, detail=f"Prior idempotent request failed: {detail}"
            )

        raise HTTPException(status_code=409, detail="Idempotent request is already in progress")

    def success(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        if self._record is None:
            return
        self._record.status = "SUCCEEDED"
        self._record.response_code = status_code
        self._record.response_payload = payload
        self.session.add(self._record)

    def failure(self, *, status_code: int, detail: str) -> None:
        if self._record is None:
            return
        self._record.status = "FAILED"
        self._record.response_code = status_code
        self._record.response_payload = {"detail": detail}
        self.session.add(self._record)

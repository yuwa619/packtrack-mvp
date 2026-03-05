from __future__ import annotations

from sqlalchemy import func, select

from ..db.models import AuditEvent


def add_audit_event(
    *,
    session,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict,
) -> None:
    event = AuditEvent(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        payload=payload,
    )
    if session.bind and session.bind.dialect.name == "sqlite":
        counter_key = "sqlite_audit_event_id_counter"
        if counter_key not in session.info:
            current_max = session.execute(
                select(func.coalesce(func.max(AuditEvent.id), 0))
            ).scalar_one()
            session.info[counter_key] = int(current_max)
        session.info[counter_key] += 1
        event.id = session.info[counter_key]
    session.add(event)

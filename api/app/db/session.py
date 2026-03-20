from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings

engine: Engine | None = None
SessionLocal: sessionmaker | None = None


def _get_engine() -> Engine:
    global engine
    if engine is None:
        engine = create_engine(settings.resolved_database_url, pool_pre_ping=True)
    return engine


def _get_session_local() -> sessionmaker:
    global SessionLocal
    if SessionLocal is None:
        SessionLocal = sessionmaker(bind=_get_engine(), autocommit=False, autoflush=False)
    return SessionLocal


@contextmanager
def db_session() -> Session:
    session_factory = _get_session_local()
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

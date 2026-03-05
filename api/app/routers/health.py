from __future__ import annotations

from urllib.error import URLError
from urllib.request import urlopen

from fastapi import APIRouter
from redis import Redis
from sqlalchemy import text

from ..config import settings
from ..db.session import db_session

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "api"}


@router.get("/ready")
def readiness() -> dict[str, str]:
    postgres_status = "ok"
    redis_status = "ok"
    minio_status = "ok"

    try:
        with db_session() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        postgres_status = "error"

    try:
        Redis.from_url(settings.redis_url).ping()
    except Exception:
        redis_status = "error"

    try:
        urlopen(f"http://{settings.minio_endpoint}/minio/health/live", timeout=2).read()
    except URLError:
        minio_status = "error"
    except Exception:
        minio_status = "error"

    return {
        "status": (
            "ok"
            if postgres_status == "ok" and redis_status == "ok" and minio_status == "ok"
            else "degraded"
        ),
        "postgres": postgres_status,
        "redis": redis_status,
        "minio": minio_status,
    }

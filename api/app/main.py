from __future__ import annotations

import logging

from fastapi import FastAPI

from .config import settings
from .routers import demo, documents, health, jobs, pipeline, reports, review

logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(title=settings.project_name)
app.include_router(health.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(demo.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(pipeline.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": settings.project_name,
        "environment": settings.environment,
        "message": "PackTrack local-first pilot API",
    }

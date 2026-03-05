from __future__ import annotations

from fastapi import FastAPI

from .routers import health, pipeline

app = FastAPI(title="PackTrack Worker")
app.include_router(health.router, prefix="/api/v1")
app.include_router(pipeline.router, prefix="/api/v1")


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "packtrack-worker", "status": "running"}

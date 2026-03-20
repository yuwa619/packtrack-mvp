from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import (
    admin,
    admin_metrics,
    batches,
    demo,
    documents,
    health,
    jobs,
    metrics,
    pipeline,
    reports,
    review,
)
from .services.ner_registry import resolve_enabled_ner_model

logging.basicConfig(level=logging.INFO, format="%(message)s")

app = FastAPI(title=settings.project_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(health.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(admin_metrics.router, prefix="/api/v1")
app.include_router(batches.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")
app.include_router(demo.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(pipeline.router, prefix="/api/v1")
app.include_router(review.router, prefix="/api/v1")
app.include_router(reports.router, prefix="/api/v1")
app.include_router(metrics.router, prefix="/api/v1")


def validate_ner_startup_gate() -> None:
    registry = resolve_enabled_ner_model(
        enabled=settings.ner_enabled,
        registry_path=settings.ner_registry_path,
        min_overall_f1=settings.ner_min_overall_f1,
        min_invoice_ref_f1=settings.ner_min_invoice_ref_f1,
    )
    if registry is None:
        return
    logging.getLogger("packtrack.api").info(
        "NER enabled with gated model: path=%s trained_at=%s overall_f1=%.4f invoice_ref_f1=%.4f",
        registry.model_path,
        registry.trained_at.isoformat(),
        registry.overall_f1,
        registry.invoice_ref_f1,
    )


@app.on_event("startup")
def _startup_validation() -> None:
    validate_ner_startup_gate()


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": settings.project_name,
        "environment": settings.environment,
        "message": "PackTrack local-first pilot API",
    }

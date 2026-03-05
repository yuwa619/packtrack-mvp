from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID


def run_mock_pipeline(document_id: UUID) -> dict[str, object]:
    # TODO: replace with Redis-backed stage orchestration and real worker execution.
    now = datetime.now(timezone.utc).isoformat()
    return {
        "document_id": str(document_id),
        "started_at": now,
        "status": "completed-mocked",
        "stages": [
            {"name": "ingest", "status": "ok", "details": "mock file registered"},
            {"name": "preprocess", "status": "ok", "details": "mock page preprocessing"},
            {
                "name": "extract",
                "status": "mocked",
                "details": "TODO: OCR + NER not implemented; returning synthetic entities.",
                "entities": [
                    {"label": "ORG_ID", "value": "123456", "confidence": 0.99},
                    {"label": "MATERIAL", "value": "Paper or cardboard", "confidence": 0.91},
                ],
            },
            {
                "name": "classify",
                "status": "mocked",
                "details": "TODO: classifier not implemented; returning static taxonomy mapping.",
                "classification": {
                    "packaging_activity": "SB",
                    "packaging_type": "HH",
                    "packaging_class": "P1",
                    "packaging_material": "Paper or cardboard",
                },
            },
            {
                "name": "report",
                "status": "queued",
                "details": "CSV export is available in API reports endpoint.",
            },
        ],
    }

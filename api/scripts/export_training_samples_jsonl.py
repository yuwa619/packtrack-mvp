from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

try:
    from app.db.models import TrainingSample
    from app.db.session import db_session
except ModuleNotFoundError:
    from api.app.db.models import TrainingSample
    from api.app.db.session import db_session


def _normalise_span(
    *,
    data_text: str,
    corrected_value: str,
    span_start: int | None,
    span_end: int | None,
) -> tuple[int | None, int | None]:
    if not data_text:
        return None, None

    needle = corrected_value.strip()
    if (
        isinstance(span_start, int)
        and isinstance(span_end, int)
        and 0 <= span_start < span_end <= len(data_text)
    ):
        candidate = data_text[span_start:span_end]
        if not needle:
            return None, None
        if needle.casefold() in candidate.casefold() or candidate.casefold() in needle.casefold():
            return span_start, span_end

    if needle:
        match_start = data_text.casefold().find(needle.casefold())
        if match_start >= 0:
            return match_start, match_start + len(needle)

    return None, None


def export_training_samples_jsonl(*, output_path: Path, reviewer: str | None = None) -> int:
    with db_session() as session:
        query = select(TrainingSample).order_by(TrainingSample.created_at.asc())
        if reviewer:
            query = query.where(TrainingSample.reviewer == reviewer)
        samples = session.execute(query).scalars().all()
        payloads: list[dict] = []
        for sample in samples:
            span_start, span_end = _normalise_span(
                data_text=sample.ocr_text or "",
                corrected_value=sample.corrected_value,
                span_start=sample.span_start,
                span_end=sample.span_end,
            )
            payloads.append(
                {
                    "id": str(sample.id),
                    "data": {
                        "text": sample.ocr_text,
                    },
                    "meta": {
                        "field_name": sample.field_name,
                        "corrected_value": sample.corrected_value,
                        "document_id": str(sample.document_id),
                        "page_number": sample.page_number,
                        "reviewer": sample.reviewer,
                        "created_at": sample.created_at.isoformat() if sample.created_at else None,
                        "source": sample.source,
                        "taxonomy_code": sample.taxonomy_code,
                        "span_start": span_start,
                        "span_end": span_end,
                    },
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    return len(payloads)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export captured review corrections to JSONL for Label Studio import."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/tmp/packtrack_training_samples.jsonl"),
        help="Output JSONL path (default: /tmp/packtrack_training_samples.jsonl)",
    )
    parser.add_argument(
        "--reviewer",
        type=str,
        default=None,
        help="Optional reviewer filter",
    )
    args = parser.parse_args()

    count = export_training_samples_jsonl(output_path=args.output, reviewer=args.reviewer)
    print(f"Exported {count} training samples to {args.output}")


if __name__ == "__main__":
    main()

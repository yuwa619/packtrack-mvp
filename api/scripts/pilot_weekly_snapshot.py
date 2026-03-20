from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from app.db.session import db_session
    from app.services.pilot_metrics import get_pilot_summary
except ModuleNotFoundError:  # pragma: no cover - fallback for repo-root execution
    from api.app.db.session import db_session
    from api.app.services.pilot_metrics import get_pilot_summary

DEFAULT_OUTPUT_DIR = Path("/tmp/packtrack_pilot_snapshots")


def _write_csv(*, path: Path, payload: dict[str, Any]) -> None:
    fieldnames = [
        "generated_at_utc",
        "window_days",
        "docs_processed_7d",
        "reports_generated_7d",
        "avg_pipeline_runtime_sec",
        "p95_pipeline_runtime_sec",
        "review_task_rate_pct",
        "extraction_coverage_pct",
        "top_failure_reasons",
        "top_suppliers_by_review_rate",
        "top_templates_by_review_rate",
    ]
    row = {
        "generated_at_utc": payload["generated_at_utc"],
        "window_days": payload["window_days"],
        "docs_processed_7d": payload["docs_processed_7d"],
        "reports_generated_7d": payload["reports_generated_7d"],
        "avg_pipeline_runtime_sec": payload["avg_pipeline_runtime_sec"],
        "p95_pipeline_runtime_sec": payload["p95_pipeline_runtime_sec"],
        "review_task_rate_pct": payload["review_task_rate_pct"],
        "extraction_coverage_pct": payload["extraction_coverage_pct"],
        "top_failure_reasons": json.dumps(payload["top_failure_reasons"], ensure_ascii=True),
        "top_suppliers_by_review_rate": json.dumps(
            payload["top_suppliers_by_review_rate"],
            ensure_ascii=True,
        ),
        "top_templates_by_review_rate": json.dumps(
            payload["top_templates_by_review_rate"],
            ensure_ascii=True,
        ),
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a 7-day pilot weekly metrics snapshot.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for snapshot files (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    generated_at = datetime.now(timezone.utc)
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    with db_session() as session:
        summary = get_pilot_summary(session=session, window_days=7)

    payload = {
        "generated_at_utc": generated_at.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "window_days": 7,
        **summary,
    }
    json_path = output_dir / f"pilot_weekly_snapshot_{timestamp}.json"
    csv_path = output_dir / f"pilot_weekly_snapshot_{timestamp}.csv"

    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _write_csv(path=csv_path, payload=payload)

    print(f"snapshot_json={json_path}")
    print(f"snapshot_csv={csv_path}")


if __name__ == "__main__":
    main()

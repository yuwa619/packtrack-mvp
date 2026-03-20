from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.app.config import settings  # noqa: E402
from api.app.db.base import Base  # noqa: E402
from api.app.db.models import (  # noqa: E402
    Classification,
    Document,
    ExtractedEntity,
    Job,
    Page,
    Report,
)
from api.app.services import ocr as ocr_service  # noqa: E402
from api.app.services.pipeline_runner import PipelineRunner  # noqa: E402
from api.app.services.storage import ObjectStorage  # noqa: E402
from api.app.services.taxonomy_loader import load_taxonomy_from_excel  # noqa: E402

FIXTURE_INDEX_PATH = Path("tests/fixtures/invoices/index.json")
FIXTURE_DIR = Path("tests/fixtures/invoices")
DEFAULT_OUTPUT_DIR = Path("/tmp/packtrack_fixture_eval")
MODES = ("heuristics", "ner")


def _resolve_workbook_path() -> Path:
    candidates = [
        Path("data/defra/UK_DEFRA.xlsx"),
        Path("data/defra/UK DEFRA.xlsx"),
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("DEFRA workbook not found")


def _load_fixture_index() -> list[dict]:
    payload = json.loads(FIXTURE_INDEX_PATH.read_text(encoding="utf-8"))
    fixtures = payload.get("fixtures", [])
    if not fixtures:
        raise ValueError("No fixtures found in tests/fixtures/invoices/index.json")
    return fixtures


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[94]


@contextmanager
def _patched_settings(**overrides):
    old_values = {name: getattr(settings, name) for name in overrides}
    try:
        for name, value in overrides.items():
            setattr(settings, name, value)
        yield
    finally:
        for name, value in old_values.items():
            setattr(settings, name, value)


def _build_docker_tesseract_shim(*, shim_path: Path, docker_bin: str, container_name: str) -> None:
    shim_code = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import subprocess
        import sys
        import uuid

        CONTAINER = {container_name!r}
        DOCKER = {docker_bin!r}

        def _detect_extension(args):
            known = {{"txt", "tsv", "hocr", "pdf", "box", "osd", "xml", "alto"}}
            for token in reversed(args):
                if token in known:
                    return token
            for idx, token in enumerate(args):
                if token != "-c" or idx + 1 >= len(args):
                    continue
                config_arg = args[idx + 1].lower()
                if "tessedit_create_tsv=1" in config_arg:
                    return "tsv"
                if "tessedit_create_hocr=1" in config_arg:
                    return "hocr"
                if "tessedit_create_pdf=1" in config_arg:
                    return "pdf"
            return "txt"

        def _run():
            if len(sys.argv) == 2 and sys.argv[1].startswith("-"):
                process = subprocess.run(
                    [DOCKER, "exec", CONTAINER, "tesseract", sys.argv[1]],
                    check=False,
                )
                return process.returncode
            if len(sys.argv) < 3:
                return 1
            source = sys.argv[1]
            output_base = sys.argv[2]
            passthrough = sys.argv[3:]
            ext = _detect_extension(passthrough)

            suffix = uuid.uuid4().hex
            remote_source = f"/tmp/{{suffix}}-source.png"
            remote_output_base = f"/tmp/{{suffix}}-out"
            remote_output = f"{{remote_output_base}}.{{ext}}"
            local_output = f"{{output_base}}.{{ext}}"

            try:
                subprocess.run(
                    [DOCKER, "cp", source, f"{{CONTAINER}}:{{remote_source}}"],
                    check=True,
                )
                process = subprocess.run(
                    [
                        DOCKER,
                        "exec",
                        CONTAINER,
                        "tesseract",
                        remote_source,
                        remote_output_base,
                        *passthrough,
                    ],
                    check=False,
                )
                if process.returncode != 0:
                    return process.returncode
                subprocess.run(
                    [DOCKER, "cp", f"{{CONTAINER}}:{{remote_output}}", local_output],
                    check=True,
                )
                return 0
            finally:
                subprocess.run(
                    [DOCKER, "exec", CONTAINER, "rm", "-f", remote_source, remote_output],
                    check=False,
                )

        raise SystemExit(_run())
        """
    )
    shim_path.write_text(shim_code, encoding="utf-8")
    shim_path.chmod(0o755)


def _resolve_tesseract_cmd(*, work_dir: Path) -> str:
    local_tesseract = shutil.which("tesseract")
    if local_tesseract:
        return local_tesseract

    docker_bin = shutil.which("docker")
    if not docker_bin:
        raise RuntimeError("tesseract not found and docker fallback unavailable")

    container_name = os.environ.get("PACKTRACK_TESSERACT_CONTAINER", "packtrack-api")
    inspect = subprocess.run(
        [docker_bin, "inspect", "-f", "{{.State.Running}}", container_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if inspect.returncode != 0 or inspect.stdout.strip() != "true":
        raise RuntimeError(
            "tesseract not found and docker fallback container is unavailable: "
            f"{container_name}"
        )

    shim_path = work_dir / "docker_tesseract_shim.py"
    _build_docker_tesseract_shim(
        shim_path=shim_path,
        docker_bin=docker_bin,
        container_name=container_name,
    )
    return str(shim_path)


def _render_metrics_text(metrics: dict) -> str:
    lines = [
        "PackTrack Fixture Evaluation",
        "",
        "fixture | ocr_pass | extraction_coverage_pct | classification_match | runtime_sec",
    ]
    for row in metrics["per_fixture"]:
        lines.append(
            " | ".join(
                [
                    row["fixture"],
                    "yes" if row["ocr_pass"] else "no",
                    f"{row['extraction_coverage_pct']:.1f}",
                    "yes" if row["classification_match"] else "no",
                    f"{row['runtime_sec']:.3f}",
                ]
            )
        )

    overall = metrics["overall"]
    lines.extend(
        [
            "",
            "overall",
            f"ocr_pass_rate_pct: {overall['ocr_pass_rate_pct']:.2f}",
            f"extraction_coverage_pct: {overall['extraction_coverage_pct']:.2f}",
            f"classification_match_rate_pct: {overall['classification_match_rate_pct']:.2f}",
            f"avg_runtime_sec: {overall['avg_runtime_sec']:.3f}",
            f"p95_runtime_sec: {overall['p95_runtime_sec']:.3f}",
            f"fixture_count: {overall['fixture_count']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_side_by_side(heuristics: dict, ner: dict) -> str:
    h = heuristics["overall"]
    n = ner["overall"]
    lines = [
        "PackTrack Fixture Evaluation (A/B)",
        "",
        "metric | heuristics | ner",
        f"ocr_pass_rate_pct | {h['ocr_pass_rate_pct']:.2f} | {n['ocr_pass_rate_pct']:.2f}",
        (
            "extraction_coverage_pct | "
            f"{h['extraction_coverage_pct']:.2f} | {n['extraction_coverage_pct']:.2f}"
        ),
        (
            "classification_match_rate_pct | "
            f"{h['classification_match_rate_pct']:.2f} | {n['classification_match_rate_pct']:.2f}"
        ),
        f"avg_runtime_sec | {h['avg_runtime_sec']:.3f} | {n['avg_runtime_sec']:.3f}",
        f"p95_runtime_sec | {h['p95_runtime_sec']:.3f} | {n['p95_runtime_sec']:.3f}",
        f"fixture_count | {h['fixture_count']} | {n['fixture_count']}",
    ]
    return "\n".join(lines) + "\n"


def _evaluate_single_mode(*, output_dir: Path, mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"Unsupported mode: {mode}")
    output_dir.mkdir(parents=True, exist_ok=True)
    db_path = output_dir / f"fixture-eval-{mode}.db"
    if db_path.exists():
        db_path.unlink()

    tesseract_cmd = _resolve_tesseract_cmd(work_dir=output_dir)
    previous_tesseract_cmd = ocr_service.pytesseract.pytesseract.tesseract_cmd
    ocr_service.pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    fixtures = _load_fixture_index()
    runtimes: list[float] = []
    per_fixture: list[dict] = []
    total_required_fields = 0
    total_present_required_fields = 0
    ocr_passes = 0
    class_matches = 0

    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    testing_session_local = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(engine)

    mode_overrides = {
        "ner_enabled": mode == "ner",
        "ner_registry_path": str(Path("data/models/spacy_ner/latest.json")),
    }
    with _patched_settings(
        minio_force_local=True,
        minio_allow_local_fallback=True,
        minio_fallback_dir=str(output_dir / "object-store"),
        **mode_overrides,
    ):
        try:
            with testing_session_local() as session:
                load_taxonomy_from_excel(session=session, excel_path=_resolve_workbook_path())
                session.commit()

            for fixture in fixtures:
                started = time.perf_counter()
                storage = ObjectStorage()

                image_name = fixture.get("fallback_image_file") or fixture["pdf_file"].replace(
                    ".pdf", ".png"
                )
                image_path = FIXTURE_DIR / image_name
                image_bytes = image_path.read_bytes()

                document_id = uuid4()
                job_id = uuid4()
                raw_uri = storage.put_bytes(
                    bucket=settings.minio_bucket_raw,
                    key=f"raw-uploads/123456/{document_id}/{image_name}",
                    data=image_bytes,
                    content_type="image/png",
                )

                with testing_session_local() as session:
                    document = Document(
                        id=document_id,
                        organisation_id=123456,
                        subsidiary_id="",
                        organisation_size="L",
                        submission_period="2025-P1",
                        original_filename=image_name,
                        mime_type="image/png",
                        file_size_bytes=len(image_bytes),
                        checksum_sha256=None,
                        uploaded_by="fixture-evaluator",
                        storage_path=raw_uri,
                        status="QUEUED",
                    )
                    job = Job(
                        id=job_id,
                        document_id=document_id,
                        organisation_id=123456,
                        status="QUEUED",
                        current_stage="QUEUED",
                        queue_name=settings.processing_queue_name,
                        attempt_count=0,
                        error_message=None,
                    )
                    session.add(document)
                    session.add(job)
                    session.commit()

                with testing_session_local() as session:
                    result = PipelineRunner(session=session, storage=ObjectStorage()).run(
                        document_id=document_id
                    )
                    session.commit()

                    pages = (
                        session.execute(
                            select(Page)
                            .where(Page.document_id == document_id)
                            .order_by(Page.page_number.asc())
                        )
                        .scalars()
                        .all()
                    )
                    ocr_text = "\n".join((page.ocr_text or "") for page in pages)
                    expected_ref = fixture["expected_extracted_fields"]["invoice_ref"]
                    ocr_pass = expected_ref in ocr_text

                    extracted = (
                        session.execute(
                            select(ExtractedEntity).where(
                                ExtractedEntity.document_id == document_id
                            )
                        )
                        .scalars()
                        .all()
                    )
                    extracted_fields = {row.field_name for row in extracted}
                    required_fields = fixture["expected_extracted_fields"]["required_fields"]
                    present_required = sum(
                        1 for field in required_fields if field in extracted_fields
                    )

                    classification = (
                        session.execute(
                            select(Classification).where(Classification.document_id == document_id)
                        )
                        .scalars()
                        .first()
                    )
                    expected_codes = set(fixture.get("expected_taxonomy_codes", []))
                    classification_match = bool(
                        classification and classification.taxonomy_code in expected_codes
                    )

                    report = session.get(Report, UUID(result.report_id))
                    if report is None or report.output_path is None:
                        raise RuntimeError(f"Report missing for fixture {fixture['pdf_file']}")

                    bucket, key = ObjectStorage.parse_uri(report.output_path)
                    storage.get_bytes(bucket=bucket, key=key)

                runtime_sec = time.perf_counter() - started
                runtimes.append(runtime_sec)
                total_required_fields += len(required_fields)
                total_present_required_fields += present_required
                if ocr_pass:
                    ocr_passes += 1
                if classification_match:
                    class_matches += 1

                per_fixture.append(
                    {
                        "fixture": fixture["pdf_file"],
                        "ocr_pass": ocr_pass,
                        "expected_invoice_ref": expected_ref,
                        "required_fields_total": len(required_fields),
                        "required_fields_present": present_required,
                        "extraction_coverage_pct": (
                            100.0 * present_required / len(required_fields)
                            if required_fields
                            else 100.0
                        ),
                        "classification_match": classification_match,
                        "expected_taxonomy_codes": sorted(expected_codes),
                        "runtime_sec": runtime_sec,
                    }
                )
        finally:
            ocr_service.pytesseract.pytesseract.tesseract_cmd = previous_tesseract_cmd

    fixture_count = len(per_fixture)
    overall = {
        "fixture_count": fixture_count,
        "ocr_pass_rate_pct": 100.0 * ocr_passes / fixture_count if fixture_count else 0.0,
        "extraction_coverage_pct": (
            100.0 * total_present_required_fields / total_required_fields
            if total_required_fields
            else 0.0
        ),
        "classification_match_rate_pct": (
            100.0 * class_matches / fixture_count if fixture_count else 0.0
        ),
        "avg_runtime_sec": statistics.mean(runtimes) if runtimes else 0.0,
        "p95_runtime_sec": _p95(runtimes),
    }
    metrics = {
        "mode": mode,
        "fixtures_source": str(FIXTURE_INDEX_PATH),
        "output_dir": str(output_dir),
        "per_fixture": per_fixture,
        "overall": overall,
    }

    metrics_json_path = output_dir / f"metrics_{mode}.json"
    metrics_txt_path = output_dir / f"metrics_{mode}.txt"
    metrics_json_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    metrics_txt_path.write_text(_render_metrics_text(metrics), encoding="utf-8")

    # Backwards compatibility for existing thresholds test.
    if mode == "heuristics":
        (output_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / "metrics.txt").write_text(_render_metrics_text(metrics), encoding="utf-8")

    return metrics


def evaluate_fixtures(*, output_dir: Path = DEFAULT_OUTPUT_DIR, mode: str = "heuristics") -> dict:
    if mode == "both":
        heuristics = _evaluate_single_mode(output_dir=output_dir, mode="heuristics")
        ner = _evaluate_single_mode(output_dir=output_dir, mode="ner")
        side_by_side = _render_side_by_side(heuristics, ner)
        side_by_side_path = output_dir / "metrics_ab.txt"
        side_by_side_path.write_text(side_by_side, encoding="utf-8")

        print(_render_metrics_text(heuristics), end="")
        print(_render_metrics_text(ner), end="")
        print(side_by_side, end="")
        print(f"metrics_json={output_dir / 'metrics_heuristics.json'}")
        print(f"metrics_json={output_dir / 'metrics_ner.json'}")
        print(f"metrics_side_by_side={side_by_side_path}")
        return {"heuristics": heuristics, "ner": ner}

    metrics = _evaluate_single_mode(output_dir=output_dir, mode=mode)
    print(_render_metrics_text(metrics), end="")
    print(f"metrics_json={output_dir / f'metrics_{mode}.json'}")
    print(f"metrics_txt={output_dir / f'metrics_{mode}.txt'}")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate PackTrack invoice fixtures.")
    parser.add_argument(
        "--mode",
        choices=["heuristics", "ner", "both"],
        default="both",
        help="Evaluation mode: heuristics only, ner only, or both side-by-side.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Output directory for metrics artefacts.",
    )
    args = parser.parse_args()
    evaluate_fixtures(output_dir=args.output_dir, mode=args.mode)

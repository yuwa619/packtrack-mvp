from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TARGET_LABELS = {
    "SUPPLIER_REF",
    "INVOICE_DATE",
    "WEIGHT_VALUE",
    "WEIGHT_UNIT",
    "PACKAGING_FORMAT",
    "MATERIAL_HINT",
    "PRODUCT_DESC",
    "SUPPLIER_NAME",
    "INVOICE_REF",
}

FIELD_TO_LABEL = {
    "supplier_ref": "SUPPLIER_REF",
    "invoice_date": "INVOICE_DATE",
    "weight_value": "WEIGHT_VALUE",
    "weight_unit": "WEIGHT_UNIT",
    "packaging_format": "PACKAGING_FORMAT",
    "material_hint": "MATERIAL_HINT",
    "product_desc": "PRODUCT_DESC",
    "supplier_name": "SUPPLIER_NAME",
    "invoice_ref": "INVOICE_REF",
}


@dataclass(frozen=True)
class SpanExample:
    start: int
    end: int
    label: str


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _looks_like_labelstudio_export(path: Path) -> bool:
    try:
        payload = _load_json(path)
    except Exception:
        return False
    if not isinstance(payload, list) or not payload:
        return False
    first = payload[0]
    if not isinstance(first, dict):
        return False
    return "data" in first and "annotations" in first


def find_latest_export_file() -> Path:
    roots = [Path("data/labels"), Path("docs/labelstudio")]
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            if _looks_like_labelstudio_export(path):
                candidates.append(path)
    if not candidates:
        raise FileNotFoundError(
            "No Label Studio export JSON found under data/labels/ or docs/labelstudio/"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _normalise_span(
    *,
    data_text: str,
    intended_text: str,
    span_start: int | None,
    span_end: int | None,
) -> tuple[int | None, int | None, str]:
    if not data_text:
        return None, None, "missing_text"

    needle = intended_text.strip()
    needle_casefold = needle.casefold()
    fallback_reason = "not_found_in_text"
    if isinstance(span_start, int) and isinstance(span_end, int):
        if 0 <= span_start < span_end <= len(data_text):
            candidate = data_text[span_start:span_end]
            candidate_casefold = candidate.casefold()
            if not needle:
                return span_start, span_end, "kept"
            if needle_casefold == candidate_casefold:
                left_ok = span_start == 0 or not data_text[span_start - 1].isalnum()
                right_ok = span_end == len(data_text) or not data_text[span_end].isalnum()
                if len(needle) <= 3 and not (left_ok and right_ok):
                    fallback_reason = "invalid_token_boundary"
                else:
                    return span_start, span_end, "kept"
            elif (
                len(needle) > 3
                and (
                    needle_casefold in candidate_casefold
                    or candidate_casefold in needle_casefold
                )
            ):
                return span_start, span_end, "kept"
        else:
            fallback_reason = "invalid_range"

    if needle:
        match_start = -1
        if any(char.isalnum() for char in needle):
            boundary_pattern = re.compile(
                rf"(?<![0-9A-Za-z_]){re.escape(needle)}(?![0-9A-Za-z_])",
                flags=re.IGNORECASE,
            )
            boundary_match = boundary_pattern.search(data_text)
            if boundary_match:
                match_start = boundary_match.start()
        if match_start < 0:
            match_start = data_text.casefold().find(needle_casefold)
        if match_start >= 0:
            return match_start, match_start + len(needle), "repaired_from_search"
        return None, None, fallback_reason

    return None, None, "missing_intended_text"


def _extract_task_spans(
    *,
    task: dict[str, Any],
    dropped_reasons: Counter[str],
    repaired_counter: Counter[str],
) -> list[SpanExample]:
    task_data = task.get("data") or {}
    task_meta = task.get("meta") or {}
    text = str(task_data.get("text") or "")
    if not text:
        dropped_reasons["missing_text"] += 1
        return []

    annotations = task.get("annotations") or []
    if not annotations:
        return []

    latest_annotation = max(
        annotations,
        key=lambda annotation: (
            str(annotation.get("updated_at") or ""),
            str(annotation.get("created_at") or ""),
            int(annotation.get("id") or 0),
        ),
    )

    spans: list[SpanExample] = []
    for result in latest_annotation.get("result") or []:
        result_type = result.get("type")
        if result_type not in {"labels", "spanlabels"}:
            continue
        value = result.get("value") or {}
        labels = value.get("labels") or []
        if not labels:
            dropped_reasons["missing_label"] += 1
            continue
        label = str(labels[0]).strip()
        if label not in TARGET_LABELS:
            dropped_reasons["unsupported_label"] += 1
            continue

        raw_start = value.get("start")
        raw_end = value.get("end")
        span_start = raw_start if isinstance(raw_start, int) else None
        span_end = raw_end if isinstance(raw_end, int) else None

        preferred_text = ""
        field_name = str(task_meta.get("field_name") or "").strip()
        corrected_value = str(task_meta.get("corrected_value") or "").strip()
        expected_label = FIELD_TO_LABEL.get(field_name)
        if corrected_value and expected_label == label:
            preferred_text = corrected_value

        intended_text = preferred_text or str(value.get("text") or "")
        if (
            not intended_text
            and span_start is not None
            and span_end is not None
            and 0 <= span_start < span_end <= len(text)
        ):
            intended_text = text[span_start:span_end]

        fixed_start, fixed_end, reason = _normalise_span(
            data_text=text,
            intended_text=intended_text,
            span_start=span_start,
            span_end=span_end,
        )
        if fixed_start is None or fixed_end is None:
            dropped_reasons[reason] += 1
            continue
        if reason == "repaired_from_search":
            repaired_counter["repaired_from_search"] += 1
        spans.append(SpanExample(start=fixed_start, end=fixed_end, label=label))

    return spans


def _split_train_dev(
    examples: list[dict[str, Any]], *, seed: int, dev_ratio: float = 0.2
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(examples) < 2:
        return examples, []

    rng = random.Random(seed)
    label_to_indices: dict[str, list[int]] = defaultdict(list)
    for idx, example in enumerate(examples):
        labels = {entity[2] for entity in example["entities"]}
        for label in labels:
            label_to_indices[label].append(idx)

    target_dev_size = max(1, int(round(len(examples) * dev_ratio)))
    dev_indices: set[int] = set()

    # Guarantee dev coverage: at least one example per label when possible.
    for _label, indices in sorted(label_to_indices.items(), key=lambda item: len(item[1])):
        if not indices:
            continue
        if len(indices) == 1:
            continue
        candidates = [idx for idx in indices if idx not in dev_indices]
        if not candidates:
            continue
        chosen = rng.choice(candidates)
        dev_indices.add(chosen)

    remaining = [idx for idx in range(len(examples)) if idx not in dev_indices]
    rng.shuffle(remaining)
    for idx in remaining:
        if len(dev_indices) >= target_dev_size:
            break
        dev_indices.add(idx)

    if len(dev_indices) == len(examples):
        dev_indices.remove(next(iter(dev_indices)))

    train = [example for idx, example in enumerate(examples) if idx not in dev_indices]
    dev = [example for idx, example in enumerate(examples) if idx in dev_indices]
    return train, dev


def _to_docbin(
    *,
    examples: list[dict[str, Any]],
    output_path: Path,
    dropped_reasons: Counter[str],
) -> Counter[str]:
    import spacy
    from spacy.tokens import DocBin
    from spacy.util import filter_spans

    nlp = spacy.blank("en")
    doc_bin = DocBin(store_user_data=True)
    label_counts: Counter[str] = Counter()
    for example in examples:
        doc = nlp.make_doc(example["text"])
        spans = []
        for start, end, label in example["entities"]:
            span = doc.char_span(start, end, label=label, alignment_mode="strict")
            if span is None:
                span = doc.char_span(start, end, label=label, alignment_mode="contract")
            if span is None:
                span = doc.char_span(start, end, label=label, alignment_mode="expand")
            if span is None:
                dropped_reasons["char_span_alignment_failed"] += 1
                continue
            spans.append(span)

        filtered = filter_spans(spans)
        if len(filtered) < len(spans):
            dropped_reasons["overlap_filtered"] += len(spans) - len(filtered)
        doc.ents = filtered
        for span in filtered:
            label_counts[span.label_] += 1
        doc_bin.add(doc)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc_bin.to_disk(output_path)
    return label_counts


def convert_labelstudio_export(
    *,
    export_path: Path,
    output_dir: Path,
    seed: int = 42,
) -> dict[str, Any]:
    payload = _load_json(export_path)
    if not isinstance(payload, list):
        raise ValueError("Label Studio export JSON must be a list of tasks")

    total_tasks = len(payload)
    annotated_tasks = 0
    dropped_reasons: Counter[str] = Counter()
    repaired_counter: Counter[str] = Counter()
    prepared_examples: list[dict[str, Any]] = []
    grouped_entities: dict[str, set[tuple[int, int, str]]] = defaultdict(set)
    grouped_counts: Counter[str] = Counter()
    tasks_with_entities = 0
    kept_spans_pre_doc = 0

    for task in payload:
        annotations = task.get("annotations") or []
        if annotations:
            annotated_tasks += 1
        spans = _extract_task_spans(
            task=task,
            dropped_reasons=dropped_reasons,
            repaired_counter=repaired_counter,
        )
        if not spans:
            continue

        text = str((task.get("data") or {}).get("text") or "")
        entities = {(span.start, span.end, span.label) for span in spans}
        kept_spans_pre_doc += len(entities)
        tasks_with_entities += 1
        grouped_entities[text].update(entities)
        grouped_counts[text] += 1

    for text, entities in grouped_entities.items():
        sorted_entities = sorted(entities, key=lambda value: (value[0], value[1], value[2]))
        repeat_count = max(1, grouped_counts[text])
        for _ in range(repeat_count):
            prepared_examples.append({"text": text, "entities": sorted_entities})

    if len(prepared_examples) == 1:
        prepared_examples.append(
            {
                "text": prepared_examples[0]["text"],
                "entities": list(prepared_examples[0]["entities"]),
            }
        )

    train_examples, dev_examples = _split_train_dev(prepared_examples, seed=seed)

    train_path = output_dir / "train.spacy"
    dev_path = output_dir / "dev.spacy"
    train_label_counts = _to_docbin(
        examples=train_examples,
        output_path=train_path,
        dropped_reasons=dropped_reasons,
    )
    dev_label_counts = _to_docbin(
        examples=dev_examples,
        output_path=dev_path,
        dropped_reasons=dropped_reasons,
    )
    all_label_counts = train_label_counts + dev_label_counts

    labels_payload = {
        "labels": sorted(all_label_counts.keys()),
        "label_counts": dict(sorted(all_label_counts.items())),
        "summary": {
            "total_tasks": total_tasks,
            "annotated_tasks": annotated_tasks,
            "tasks_with_entities": tasks_with_entities,
            "kept_spans": int(sum(all_label_counts.values())),
            "kept_spans_pre_docbin": kept_spans_pre_doc,
            "train_docs": len(train_examples),
            "dev_docs": len(dev_examples),
            "prepared_docs": len(prepared_examples),
            "unique_text_docs": len(grouped_entities),
            "repaired_spans": int(repaired_counter["repaired_from_search"]),
            "dropped_spans_by_reason": dict(sorted(dropped_reasons.items())),
            "source_export": str(export_path),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "labels.json").write_text(json.dumps(labels_payload, indent=2), encoding="utf-8")
    return labels_payload["summary"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Label Studio export JSON into spaCy DocBin train/dev datasets."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=(
            "Path to Label Studio export JSON. Defaults to the latest export under "
            "data/labels or docs/labelstudio."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/training/spacy"),
        help="Output directory for train.spacy, dev.spacy and labels.json",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for train/dev split")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    export_path = args.input or find_latest_export_file()

    summary = convert_labelstudio_export(
        export_path=export_path,
        output_dir=args.output_dir,
        seed=args.seed,
    )
    print(f"Source export: {export_path}")
    print(
        "Summary: "
        f"total_tasks={summary['total_tasks']} "
        f"annotated_tasks={summary['annotated_tasks']} "
        f"tasks_with_entities={summary['tasks_with_entities']} "
        f"kept_spans={summary['kept_spans']} "
        f"train_docs={summary['train_docs']} "
        f"dev_docs={summary['dev_docs']}"
    )
    print(f"Dropped spans by reason: {summary['dropped_spans_by_reason']}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.app.services.ner_registry import write_latest_registry


def _docbin_label_counts(path: Path) -> dict[str, int]:
    from collections import Counter

    import spacy
    from spacy.tokens import DocBin

    nlp = spacy.blank("en")
    docs = list(DocBin().from_disk(path).get_docs(nlp.vocab))
    counter: Counter[str] = Counter()
    for doc in docs:
        for ent in doc.ents:
            counter[ent.label_] += 1
    return dict(counter)


def _validate_training_inputs(
    *,
    config_path: Path,
    train_data_path: Path,
    dev_data_path: Path,
    labels_path: Path,
) -> dict[str, Any]:
    import spacy

    config = spacy.util.load_config(config_path)
    pipeline = list(config["nlp"]["pipeline"])
    if "ner" not in pipeline:
        raise ValueError("spaCy config pipeline must include 'ner'")
    if "ner" not in config["components"]:
        raise ValueError("spaCy config must include [components.ner]")

    labels_payload = json.loads(labels_path.read_text(encoding="utf-8"))
    labels_json = {str(label) for label in labels_payload.get("labels", [])}
    if not labels_json:
        raise ValueError("labels.json has no labels; cannot train NER")

    train_counts = _docbin_label_counts(train_data_path)
    dev_counts = _docbin_label_counts(dev_data_path)
    train_labels = set(train_counts.keys())
    dev_labels = set(dev_counts.keys())
    if sum(dev_counts.values()) == 0:
        raise ValueError("dev.spacy contains zero entities; training/evaluation cannot proceed")

    missing_in_train = sorted(labels_json - train_labels)
    if missing_in_train:
        raise ValueError(
            f"labels.json contains labels missing in train.spacy: {missing_in_train}"
        )

    return {
        "labels_json": sorted(labels_json),
        "train_counts": train_counts,
        "dev_counts": dev_counts,
        "dev_missing_labels": sorted(labels_json - dev_labels),
        "pipeline": pipeline,
    }


def _evaluate_model(*, model_path: Path, dev_data_path: Path) -> dict[str, Any]:
    import spacy
    from spacy.scorer import Scorer
    from spacy.tokens import DocBin
    from spacy.training import Example

    nlp = spacy.load(model_path)
    doc_bin = DocBin().from_disk(dev_data_path)
    gold_docs = list(doc_bin.get_docs(nlp.vocab))
    if not gold_docs:
        return {
            "overall": {"precision": 0.0, "recall": 0.0, "f1": 0.0},
            "per_label": {},
            "dev_docs": 0,
        }

    predicted_docs = list(nlp.pipe([doc.text for doc in gold_docs]))
    examples = [
        Example(predicted, gold)
        for predicted, gold in zip(predicted_docs, gold_docs, strict=False)
    ]
    scores = Scorer().score(examples)
    predicted_entities = sum(len(doc.ents) for doc in predicted_docs)
    gold_entities = sum(len(doc.ents) for doc in gold_docs)
    per_label_scores: dict[str, dict[str, float]] = {}
    for label, values in sorted(scores.get("ents_per_type", {}).items()):
        per_label_scores[label] = {
            "precision": float(values.get("p", 0.0)),
            "recall": float(values.get("r", 0.0)),
            "f1": float(values.get("f", 0.0)),
        }

    return {
        "overall": {
            "precision": float(scores.get("ents_p", 0.0)),
            "recall": float(scores.get("ents_r", 0.0)),
            "f1": float(scores.get("ents_f", 0.0)),
        },
        "per_label": per_label_scores,
        "dev_docs": len(gold_docs),
        "predicted_entities": predicted_entities,
        "gold_entities": gold_entities,
    }


def train_spacy_ner(
    *,
    config_path: Path,
    train_data_path: Path,
    dev_data_path: Path,
    labels_path: Path,
    output_root: Path,
    max_epochs: int,
) -> tuple[Path, dict[str, Any]]:
    from spacy.cli.train import train as spacy_train

    if not train_data_path.exists():
        raise FileNotFoundError(f"Training DocBin not found: {train_data_path}")
    if not dev_data_path.exists():
        raise FileNotFoundError(f"Dev DocBin not found: {dev_data_path}")
    if not config_path.exists():
        raise FileNotFoundError(f"spaCy config not found: {config_path}")
    if not labels_path.exists():
        raise FileNotFoundError(f"labels.json not found: {labels_path}")

    input_validation = _validate_training_inputs(
        config_path=config_path,
        train_data_path=train_data_path,
        dev_data_path=dev_data_path,
        labels_path=labels_path,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_output_dir = output_root / timestamp
    output_root.mkdir(parents=True, exist_ok=True)

    spacy_train(
        config_path=config_path,
        output_path=run_output_dir,
        use_gpu=-1,
        overrides={
            "paths.train": str(train_data_path),
            "paths.dev": str(dev_data_path),
            "training.max_epochs": max_epochs,
            "training.eval_frequency": 10,
        },
    )

    model_path = run_output_dir / "model-best"
    if not model_path.exists():
        model_path = run_output_dir / "model-last"
    if not model_path.exists():
        raise RuntimeError("Training completed but no model output directory was created")

    metrics = _evaluate_model(model_path=model_path, dev_data_path=dev_data_path)
    metrics["input_validation"] = input_validation

    trained_labels = sorted(spacy_load_labels(model_path))
    labels_json = sorted(input_validation["labels_json"])
    if trained_labels != labels_json:
        raise ValueError(
            "Trained model labels do not match labels.json "
            f"(model={trained_labels}, labels_json={labels_json})"
        )

    latest_payload = write_latest_registry(
        registry_path=output_root / "latest.json",
        model_path=model_path,
        trained_at=datetime.now(timezone.utc),
        overall_f1=float(metrics["overall"]["f1"]),
        per_label_f1={
            label: float(values["f1"]) for label, values in metrics["per_label"].items()
        },
        labels=labels_json,
    )
    metrics["latest_registry"] = latest_payload
    metrics_output_path = run_output_dir / "metrics.json"
    metrics_output_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return run_output_dir, metrics


def spacy_load_labels(model_path: Path) -> set[str]:
    import spacy

    nlp = spacy.load(model_path)
    ner = nlp.get_pipe("ner")
    return set(ner.labels)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train a spaCy NER model for PackTrack labels.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("api/training/spacy_config.cfg"),
        help="Path to spaCy config (default tok2vec config).",
    )
    parser.add_argument(
        "--train-data",
        type=Path,
        default=Path("data/training/spacy/train.spacy"),
        help="Path to train DocBin.",
    )
    parser.add_argument(
        "--dev-data",
        type=Path,
        default=Path("data/training/spacy/dev.spacy"),
        help="Path to dev DocBin.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data/models/spacy_ner"),
        help="Root directory for timestamped model outputs.",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path("data/training/spacy/labels.json"),
        help="Path to labels.json emitted by converter.",
    )
    parser.add_argument(
        "--max-epochs",
        type=int,
        default=40,
        help="Training epochs (default: 40).",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    output_dir, metrics = train_spacy_ner(
        config_path=args.config,
        train_data_path=args.train_data,
        dev_data_path=args.dev_data,
        labels_path=args.labels,
        output_root=args.output_root,
        max_epochs=args.max_epochs,
    )

    print(f"Model output: {output_dir}")
    input_validation = metrics["input_validation"]
    print(f"Config pipeline: {input_validation['pipeline']}")
    print(f"Labels from labels.json: {input_validation['labels_json']}")
    print(f"Train label counts: {input_validation['train_counts']}")
    print(f"Dev label counts: {input_validation['dev_counts']}")
    print(f"Updated registry: {Path(args.output_root) / 'latest.json'}")
    overall = metrics["overall"]
    print(
        "Overall metrics: "
        f"precision={overall['precision']:.4f} "
        f"recall={overall['recall']:.4f} "
        f"f1={overall['f1']:.4f}"
    )
    print(
        "Entity counts: "
        f"predicted={metrics['predicted_entities']} "
        f"gold={metrics['gold_entities']}"
    )
    print("Per-label metrics:")
    for label, values in metrics["per_label"].items():
        print(
            f"  {label}: "
            f"precision={values['precision']:.4f} "
            f"recall={values['recall']:.4f} "
            f"f1={values['f1']:.4f}"
        )


if __name__ == "__main__":
    main()

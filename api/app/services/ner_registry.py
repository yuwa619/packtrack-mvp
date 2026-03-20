from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NERModelRegistryEntry:
    model_path: str
    trained_at: datetime
    overall_f1: float
    per_label_f1: dict[str, float]
    labels: list[str]

    @property
    def invoice_ref_f1(self) -> float:
        return float(self.per_label_f1.get("INVOICE_REF", 0.0))

    def to_payload(self) -> dict[str, Any]:
        return {
            "model_path": self.model_path,
            "trained_at": self.trained_at.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "overall_f1": float(self.overall_f1),
            "per_label_f1": {label: float(score) for label, score in self.per_label_f1.items()},
            "labels": list(self.labels),
        }


def _parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_latest_registry(path: str | Path) -> NERModelRegistryEntry:
    registry_path = Path(path)
    if not registry_path.exists():
        raise FileNotFoundError(f"NER model registry not found: {registry_path}")

    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"NER model registry must be a JSON object: {registry_path}")

    required_keys = {"model_path", "trained_at", "overall_f1", "per_label_f1", "labels"}
    missing = sorted(required_keys - set(payload.keys()))
    if missing:
        raise ValueError(f"NER model registry missing keys {missing}: {registry_path}")

    per_label_raw = payload.get("per_label_f1")
    if not isinstance(per_label_raw, dict):
        raise ValueError(f"NER model registry per_label_f1 must be an object: {registry_path}")
    labels_raw = payload.get("labels")
    if not isinstance(labels_raw, list):
        raise ValueError(f"NER model registry labels must be a list: {registry_path}")

    return NERModelRegistryEntry(
        model_path=str(payload["model_path"]),
        trained_at=_parse_utc_timestamp(str(payload["trained_at"])),
        overall_f1=float(payload["overall_f1"]),
        per_label_f1={str(label): float(score) for label, score in per_label_raw.items()},
        labels=[str(label) for label in labels_raw],
    )


def validate_quality_gate(
    *,
    registry: NERModelRegistryEntry,
    min_overall_f1: float,
    min_invoice_ref_f1: float,
) -> None:
    if registry.overall_f1 < min_overall_f1:
        raise ValueError(
            "NER model gate failed: overall_f1 "
            f"{registry.overall_f1:.4f} < {min_overall_f1:.4f}"
        )
    invoice_ref_f1 = registry.invoice_ref_f1
    if invoice_ref_f1 < min_invoice_ref_f1:
        raise ValueError(
            "NER model gate failed: INVOICE_REF F1 "
            f"{invoice_ref_f1:.4f} < {min_invoice_ref_f1:.4f}"
        )


def resolve_enabled_ner_model(
    *,
    enabled: bool,
    registry_path: str | Path,
    min_overall_f1: float,
    min_invoice_ref_f1: float,
) -> NERModelRegistryEntry | None:
    if not enabled:
        return None
    registry = load_latest_registry(registry_path)
    validate_quality_gate(
        registry=registry,
        min_overall_f1=min_overall_f1,
        min_invoice_ref_f1=min_invoice_ref_f1,
    )
    return registry


def write_latest_registry(
    *,
    registry_path: str | Path,
    model_path: str | Path,
    trained_at: datetime,
    overall_f1: float,
    per_label_f1: dict[str, float],
    labels: list[str],
) -> dict[str, Any]:
    trained_at_utc = trained_at.astimezone(timezone.utc)
    entry = NERModelRegistryEntry(
        model_path=str(model_path),
        trained_at=trained_at_utc,
        overall_f1=float(overall_f1),
        per_label_f1={str(label): float(score) for label, score in per_label_f1.items()},
        labels=[str(label) for label in labels],
    )
    payload = entry.to_payload()
    target = Path(registry_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload

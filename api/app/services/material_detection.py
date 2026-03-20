from __future__ import annotations

import re
from dataclasses import dataclass

from .extraction_v1 import ExtractedCandidate


@dataclass
class MaterialDetection:
    material_key: str
    packaging_material: str
    packaging_material_subtype: str | None
    confidence: float
    source: str


_MATERIAL_RULES: list[tuple[str, str, str | None, tuple[str, ...]]] = [
    (
        "Plastic Rigid",
        "Plastic",
        "Rigid",
        ("rigid plastic", "pet", "hdpe", "pp", "tray", "bottle", "tub"),
    ),
    (
        "Plastic Flexible",
        "Plastic",
        "Flexible",
        ("flexible plastic", "film", "ldpe", "wrap", "pouch", "bag"),
    ),
    (
        "Paper/Cardboard",
        "Paper or cardboard",
        None,
        ("paper", "cardboard", "corrugated", "carton", "boxboard"),
    ),
    (
        "Aluminium",
        "Aluminium",
        None,
        ("aluminium", "aluminum", "alu", "foil"),
    ),
    (
        "Steel",
        "Steel",
        None,
        ("steel", "tinplate", "tin can", "ferrous"),
    ),
    (
        "Wood",
        "Wood",
        None,
        ("wood", "timber", "pallet"),
    ),
    (
        "Glass",
        "Glass",
        None,
        ("glass", "jar", "bottle glass"),
    ),
    (
        "Other",
        "Other",
        None,
        ("other material", "composite", "mixed material"),
    ),
]


def get_material_options() -> list[dict]:
    """Return material options derived from ``_MATERIAL_RULES``.

    The frontend review UI should call this endpoint instead of maintaining
    a hardcoded duplicate of the material list.
    """
    options: list[dict] = []
    for material_key, material, subtype, _keywords in _MATERIAL_RULES:
        key = material_key.lower().replace("/", "-").replace(" ", "-")
        options.append(
            {
                "key": key,
                "material_key": material_key,
                "label": material_key,
                "material": material,
                "subtype": subtype,
                "taxonomy_category": "Material",
                "taxonomy_code": material,
            }
        )
    return options


def detect_materials(
    *,
    page_texts: list[str],
    extracted_candidates: list[ExtractedCandidate],
) -> list[MaterialDetection]:
    corpus_fragments = [text for text in page_texts if text]
    ner_weight = 0.0
    for candidate in extracted_candidates:
        value = (candidate.normalized_value or candidate.raw_value or "").strip()
        if not value:
            continue
        corpus_fragments.append(value)
        if candidate.field_name in {"material_hint", "packaging_format"}:
            ner_weight += 0.1

    corpus = "\n".join(corpus_fragments).lower()
    if not corpus:
        return []

    detections: list[MaterialDetection] = []
    for material_key, packaging_material, packaging_material_subtype, keywords in _MATERIAL_RULES:
        hit_count = 0
        for keyword in keywords:
            # Use word-boundary matching to avoid false positives from
            # substring hits (e.g. "pp" matching "supplier").
            if re.search(r"\b" + re.escape(keyword) + r"\b", corpus):
                hit_count += 1
        if hit_count == 0:
            continue

        confidence = min(0.99, 0.55 + (0.12 * hit_count) + ner_weight)
        detections.append(
            MaterialDetection(
                material_key=material_key,
                packaging_material=packaging_material,
                packaging_material_subtype=packaging_material_subtype,
                confidence=round(confidence, 4),
                source="auto",
            )
        )

    return detections

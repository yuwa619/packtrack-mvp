from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ExtractedEntity, Page, TaxonomyCode


@dataclass
class RuleMatch:
    category: str
    code: str
    score: float
    rule_id: str
    reason: str


@dataclass
class ClassificationDecision:
    taxonomy_category: str
    taxonomy_code: str
    taxonomy_version: str
    packaging_activity: str
    packaging_type: str
    packaging_class: str
    packaging_material: str
    packaging_material_subtype: str
    packaging_material_weight: float | None
    confidence: float
    rule_id: str
    reason: str
    candidates: list[dict[str, Any]]


RULES = [
    # Packaging Activity
    (
        "rule.activity.brand",
        "Packaging Activity",
        "SB",
        r"\bbrand\b|supplied under your brand",
        0.95,
        "brand supply",
    ),
    (
        "rule.activity.packed",
        "Packaging Activity",
        "PF",
        r"\bpacked\b|\bfilled\b",
        0.92,
        "packed/filled",
    ),
    ("rule.activity.imported", "Packaging Activity", "IM", r"\bimport(?:ed)?\b", 0.92, "imported"),
    ("rule.activity.empty", "Packaging Activity", "SE", r"\bempty\b", 0.9, "supplied empty"),
    (
        "rule.activity.hired",
        "Packaging Activity",
        "HL",
        r"\bhired\b|\bloaned\b",
        0.9,
        "hired/loaned",
    ),
    (
        "rule.activity.marketplace",
        "Packaging Activity",
        "OM",
        r"online marketplace",
        0.9,
        "online marketplace",
    ),
    # Packaging Type
    ("rule.type.household", "Packaging Type", "HH", r"\bhousehold\b", 0.93, "household"),
    (
        "rule.type.nonhousehold",
        "Packaging Type",
        "NH",
        r"\bnon[- ]household\b|\bbusiness\b",
        0.92,
        "non-household",
    ),
    (
        "rule.type.hdc",
        "Packaging Type",
        "HDC",
        r"household drinks container",
        0.9,
        "household drinks container",
    ),
    (
        "rule.type.ndc",
        "Packaging Type",
        "NDC",
        r"non[- ]household drinks container",
        0.9,
        "non-household drinks container",
    ),
    # Packaging Class
    ("rule.class.primary", "Packaging Class", "P1", r"\bprimary\b", 0.93, "primary packaging"),
    (
        "rule.class.secondary",
        "Packaging Class",
        "P2",
        r"\bsecondary\b",
        0.93,
        "secondary packaging",
    ),
    ("rule.class.shipment", "Packaging Class", "P3", r"\bshipment\b", 0.91, "shipment packaging"),
    ("rule.class.tertiary", "Packaging Class", "P4", r"\btertiary\b", 0.91, "tertiary packaging"),
    # Material
    ("rule.material.plastic", "Material", "Plastic", r"\bplastic\b", 0.95, "plastic material"),
    (
        "rule.material.paper",
        "Material",
        "Paper or cardboard",
        r"\bpaper\b|\bcardboard\b",
        0.94,
        "paper/cardboard material",
    ),
    ("rule.material.glass", "Material", "Glass", r"\bglass\b", 0.93, "glass material"),
    ("rule.material.wood", "Material", "Wood", r"\bwood\b", 0.92, "wood material"),
    (
        "rule.material.aluminium",
        "Material",
        "Aluminium",
        r"\baluminium\b",
        0.92,
        "aluminium material",
    ),
    ("rule.material.steel", "Material", "Steel", r"\bsteel\b", 0.92, "steel material"),
    # Intentionally invalid mapping; should be rejected by taxonomy validation.
    ("rule.material.bioplastic", "Material", "BIO", r"\bbioplastic\b", 0.96, "invalid test rule"),
]


class ClassificationServiceV1:
    def __init__(self, *, session: Session) -> None:
        self.session = session

    def classify_document(self, *, document_id) -> ClassificationDecision:
        taxonomy_rows = (
            self.session.execute(select(TaxonomyCode).where(TaxonomyCode.active.is_(True)))
            .scalars()
            .all()
        )
        if not taxonomy_rows:
            raise ValueError("taxonomy_codes table is empty; classification cannot proceed")

        taxonomy = self._build_taxonomy_map(taxonomy_rows)
        taxonomy_version = sorted({row.source_sheet for row in taxonomy_rows})[0]

        text = self._build_source_text(document_id=document_id)
        matches = self._match_rules(text=text, taxonomy=taxonomy)

        activity, activity_ambiguous = self._select_code(
            category="Packaging Activity", taxonomy=taxonomy, matches=matches
        )
        packaging_type, type_ambiguous = self._select_code(
            category="Packaging Type", taxonomy=taxonomy, matches=matches
        )
        packaging_class, class_ambiguous = self._select_code(
            category="Packaging Class", taxonomy=taxonomy, matches=matches
        )
        material, material_ambiguous = self._select_code(
            category="Material", taxonomy=taxonomy, matches=matches
        )

        subtype = self._classify_plastic_subtype(
            text=text, taxonomy=taxonomy, material_code=material.code
        )

        weight_entity = (
            self.session.execute(
                select(ExtractedEntity)
                .where(
                    ExtractedEntity.document_id == document_id,
                    ExtractedEntity.field_name == "weight_value",
                )
                .order_by(ExtractedEntity.confidence.desc().nullslast())
                .limit(1)
            )
            .scalars()
            .first()
        )
        weight_value = None
        if weight_entity and weight_entity.normalized_value:
            try:
                weight_value = float(weight_entity.normalized_value)
            except ValueError:
                weight_value = None

        ambiguous = any([activity_ambiguous, type_ambiguous, class_ambiguous, material_ambiguous])
        base_confidence = min(
            activity.score, packaging_type.score, packaging_class.score, material.score
        )
        confidence = min(base_confidence, 0.84) if ambiguous else base_confidence

        top_candidates = self._top_candidates(
            matches=matches,
            taxonomy=taxonomy,
            ambiguous_categories=[
                category
                for category, is_ambiguous in [
                    ("Packaging Activity", activity_ambiguous),
                    ("Packaging Type", type_ambiguous),
                    ("Packaging Class", class_ambiguous),
                    ("Material", material_ambiguous),
                ]
                if is_ambiguous
            ],
        )

        selected_rule_ids = [
            activity.rule_id,
            packaging_type.rule_id,
            packaging_class.rule_id,
            material.rule_id,
        ]
        selected_reasons = [
            activity.reason,
            packaging_type.reason,
            packaging_class.reason,
            material.reason,
        ]

        return ClassificationDecision(
            taxonomy_category="Material",
            taxonomy_code=material.code,
            taxonomy_version=taxonomy_version,
            packaging_activity=activity.code,
            packaging_type=packaging_type.code,
            packaging_class=packaging_class.code,
            packaging_material=material.code,
            packaging_material_subtype=subtype,
            packaging_material_weight=weight_value,
            confidence=confidence,
            rule_id="|".join(selected_rule_ids),
            reason="; ".join(selected_reasons),
            candidates=top_candidates,
        )

    def _build_source_text(self, *, document_id) -> str:
        page_texts = (
            self.session.execute(select(Page.ocr_text).where(Page.document_id == document_id))
            .scalars()
            .all()
        )
        extracted_values = self.session.execute(
            select(ExtractedEntity.raw_value, ExtractedEntity.normalized_value).where(
                ExtractedEntity.document_id == document_id
            )
        ).all()

        chunks = [text for text in page_texts if text]
        for raw_value, normalized_value in extracted_values:
            if raw_value:
                chunks.append(raw_value)
            if normalized_value:
                chunks.append(normalized_value)
        return "\n".join(chunks).lower()

    @staticmethod
    def _build_taxonomy_map(taxonomy_rows: list[TaxonomyCode]) -> dict[str, set[str]]:
        taxonomy: dict[str, set[str]] = {}
        for row in taxonomy_rows:
            taxonomy.setdefault(row.category, set()).add(row.code)
        return taxonomy

    def _match_rules(self, *, text: str, taxonomy: dict[str, set[str]]) -> list[RuleMatch]:
        matches: list[RuleMatch] = []
        for rule_id, category, code, pattern, score, reason in RULES:
            if not re.search(pattern, text, flags=re.IGNORECASE):
                continue
            if code not in taxonomy.get(category, set()):
                continue
            matches.append(
                RuleMatch(
                    category=category,
                    code=code,
                    score=score,
                    rule_id=rule_id,
                    reason=reason,
                )
            )
        return matches

    def _select_code(
        self,
        *,
        category: str,
        taxonomy: dict[str, set[str]],
        matches: list[RuleMatch],
    ) -> tuple[RuleMatch, bool]:
        category_matches = [match for match in matches if match.category == category]

        if not category_matches:
            fallback = sorted(taxonomy.get(category, set()))[0]
            return (
                RuleMatch(
                    category=category,
                    code=fallback,
                    score=0.3,
                    rule_id=f"rule.fallback.{category.lower().replace(' ', '_')}",
                    reason="fallback to valid taxonomy code",
                ),
                True,
            )

        unique_codes = {match.code for match in category_matches}
        ranked = sorted(category_matches, key=lambda match: (-match.score, match.code))
        selected = ranked[0]
        ambiguous = len(unique_codes) > 1 and (
            len(ranked) == 1 or (ranked[0].score - ranked[1].score) < 0.15
        )
        return selected, ambiguous

    def _top_candidates(
        self,
        *,
        matches: list[RuleMatch],
        taxonomy: dict[str, set[str]],
        ambiguous_categories: list[str],
    ) -> list[dict[str, Any]]:
        candidate_pool = [
            match for match in matches if match.category in ambiguous_categories
        ] or matches

        dedup: dict[tuple[str, str], RuleMatch] = {}
        for match in candidate_pool:
            key = (match.category, match.code)
            if key not in dedup or match.score > dedup[key].score:
                dedup[key] = match

        ranked = sorted(
            dedup.values(), key=lambda match: (-match.score, match.category, match.code)
        )

        if len(ranked) < 3:
            for category in ["Material", "Packaging Activity", "Packaging Type", "Packaging Class"]:
                for code in sorted(taxonomy.get(category, set())):
                    key = (category, code)
                    if key in dedup:
                        continue
                    ranked.append(
                        RuleMatch(
                            category=category,
                            code=code,
                            score=0.2,
                            rule_id="rule.fallback.candidate",
                            reason="fallback candidate from taxonomy",
                        )
                    )
                    dedup[key] = ranked[-1]
                    if len(ranked) >= 3:
                        break
                if len(ranked) >= 3:
                    break

        return [
            {
                "category": match.category,
                "code": match.code,
                "score": round(match.score, 4),
                "rule_id": match.rule_id,
                "reason": match.reason,
            }
            for match in ranked[:3]
        ]

    @staticmethod
    def _classify_plastic_subtype(
        *, text: str, taxonomy: dict[str, set[str]], material_code: str
    ) -> str:
        if material_code != "Plastic":
            return ""
        subtypes = taxonomy.get("Plastic Sub-type", set())
        if "Rigid" in subtypes and re.search(r"\brigid\b", text, flags=re.IGNORECASE):
            return "Rigid"
        if "Flexible" in subtypes and re.search(r"\bflexible\b", text, flags=re.IGNORECASE):
            return "Flexible"
        return ""

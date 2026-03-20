from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

from .extraction_v1 import normalize_date_to_iso

DOCUMENT_TYPE_INVOICE = "commercial_packaging_invoice"
DOCUMENT_TYPE_NOTICE_OF_LIABILITY = "notice_of_liability"
DOCUMENT_TYPE_UNKNOWN = "unknown"

_INVOICE_HINTS = ("PACKAGING INVOICE",)
_NOTICE_HINTS = (
    "NOTICE OF LIABILITY",
    "PRODUCER INFORMATION",
    "FEE CALCULATION BREAKDOWN",
    "PACKUK",
)
_DATE_VALUE = (
    r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{2,4}"
    r"|[A-Za-z]{3,9}\s+[0-9]{1,2},?\s+[0-9]{2,4}"
)
_INVOICE_DATE_PATTERNS = [
    re.compile(rf"\binvoice\s*date\s*[:#-]?\s*({_DATE_VALUE})", re.IGNORECASE),
    re.compile(rf"\bdate\s*[:#-]?\s*({_DATE_VALUE})", re.IGNORECASE),
]
_NOTICE_DATE_PATTERNS = [
    re.compile(rf"\bdate\s*[:#-]?\s*({_DATE_VALUE})", re.IGNORECASE),
]
_COUNTRY_PATTERN = re.compile(r"\b(UK|UNITED\s+KINGDOM|GREAT\s+BRITAIN)\b", re.IGNORECASE)
_FEE_BREAKDOWN_MARKER = "FEE CALCULATION BREAKDOWN"
_NUMERIC_PATTERN = re.compile(r"(?<![A-Za-z])([0-9]+(?:\.[0-9]+)?)")
_QUOTE_ONE_PREFIX_RE = re.compile(r"^[\"'`“”‘’|!Il]+0+$")
_CURRENCY_PREFIXES = ("£", "$", "€")

_NOTICE_MATERIAL_MAP = {
    "PLASTIC": ("Plastic", None),
    "CARDBOARD": ("Paper or cardboard", None),
    "GLASS": ("Glass", None),
    "ALUMINIUM": ("Aluminium", None),
}
_NOTICE_MATERIAL_VARIANTS = {
    "Plastic": ("PLASTIC", "PLAS", "PIN"),
    "Paper or cardboard": ("CARDBOARD", "CONCBOWRD", "CARDBOWRD", "CARDBOARD"),
    "Glass": ("GLASS", "GASS"),
    "Aluminium": ("ALUMINIUM", "ALUMINUM", "AUERAM", "ALU"),
}
_NOTICE_MATERIAL_ORDER = ["Plastic", "Paper or cardboard", "Glass", "Aluminium"]
_NOTICE_STOP_MARKERS = ("TOTAL AMOUNT", "PAYMENT TERMS", "OFFICIAL REFERENCE")


@dataclass
class StructuredMaterialRow:
    material_key: str
    packaging_material: str
    packaging_material_subtype: str | None
    weight_value: Decimal | None
    weight_unit: str | None
    confidence: float
    source: str
    provenance: dict = field(default_factory=dict)


@dataclass
class DocumentInsights:
    document_type: str = DOCUMENT_TYPE_UNKNOWN
    document_date: str | None = None
    inferred_country_code: str | None = None
    country_inference_source: str | None = None
    material_rows: list[StructuredMaterialRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DocumentInsightsService:
    def inspect(self, *, page_texts: list[str]) -> DocumentInsights:
        corpus = "\n".join(text for text in page_texts if text)
        lines = [line.strip() for line in corpus.splitlines() if line.strip()]
        document_type = self._detect_document_type(corpus)
        document_date = self._extract_document_date(
            corpus=corpus,
            lines=lines,
            document_type=document_type,
        )
        inferred_country_code = None
        country_inference_source = None
        if _COUNTRY_PATTERN.search(corpus) or "PACKUK" in corpus.upper():
            inferred_country_code = "GB"
            country_inference_source = "inferred_from_text"

        material_rows: list[StructuredMaterialRow] = []
        warnings: list[str] = []
        if document_type == DOCUMENT_TYPE_NOTICE_OF_LIABILITY:
            material_rows = self._extract_notice_material_rows(lines=lines)
            if material_rows:
                warnings.append(
                    "PackUK tonnage extracted from fee breakdown "
                    "(source unit: tonnes, stored as kg)."
                )

        return DocumentInsights(
            document_type=document_type,
            document_date=document_date,
            inferred_country_code=inferred_country_code,
            country_inference_source=country_inference_source,
            material_rows=material_rows,
            warnings=warnings,
        )

    def _detect_document_type(self, corpus: str) -> str:
        upper = corpus.upper()
        if any(hint in upper for hint in _NOTICE_HINTS):
            return DOCUMENT_TYPE_NOTICE_OF_LIABILITY
        if any(hint in upper for hint in _INVOICE_HINTS):
            return DOCUMENT_TYPE_INVOICE
        if "INVOICE" in upper:
            return DOCUMENT_TYPE_INVOICE
        return DOCUMENT_TYPE_UNKNOWN

    def _extract_document_date(
        self,
        *,
        corpus: str,
        lines: list[str],
        document_type: str,
    ) -> str | None:
        patterns = (
            _NOTICE_DATE_PATTERNS
            if document_type == DOCUMENT_TYPE_NOTICE_OF_LIABILITY
            else _INVOICE_DATE_PATTERNS
        )
        for pattern in patterns:
            match = pattern.search(corpus)
            if not match:
                continue
            normalized = normalize_date_to_iso(match.group(1))
            if normalized:
                return normalized
        if document_type == DOCUMENT_TYPE_NOTICE_OF_LIABILITY:
            repaired = self._repair_notice_date(lines=lines)
            if repaired:
                return repaired
        return None

    def _extract_notice_material_rows(self, *, lines: list[str]) -> list[StructuredMaterialRow]:
        in_breakdown = False
        table_started = False
        row_index = 0
        materials: dict[str, StructuredMaterialRow] = {}
        for line in lines:
            upper_line = line.upper()
            if _FEE_BREAKDOWN_MARKER in upper_line or self._is_notice_breakdown_line(upper_line):
                in_breakdown = True
                continue
            if not in_breakdown:
                continue
            if any(marker in upper_line for marker in _NOTICE_STOP_MARKERS):
                break
            if not table_started and ("TONNAGE" in upper_line and "CATEGORY" in upper_line):
                table_started = True
                continue
            if not table_started and not self._looks_like_notice_material_row(upper_line):
                continue
            table_started = True

            material = self._match_notice_material(line=line, row_index=row_index)
            if material is None:
                continue
            tonnage = self._extract_notice_tonnage(line=line)
            packaging_material, subtype = self._material_mapping(material)
            material_key = (
                packaging_material if subtype is None else f"{packaging_material} {subtype}"
            )
            materials[packaging_material] = StructuredMaterialRow(
                material_key=material_key,
                packaging_material=packaging_material,
                packaging_material_subtype=subtype,
                weight_value=tonnage,
                weight_unit="tonnes" if tonnage is not None else None,
                confidence=0.99 if tonnage is not None else 0.86,
                source="auto",
                provenance={
                    "method": "notice_of_liability_fee_breakdown",
                    "line_text": line,
                    "source_unit": "tonnes",
                    "row_index": row_index + 1,
                },
            )
            row_index += 1
        if materials:
            return list(materials.values())

        row_index = 0
        for line in lines:
            upper_line = line.upper()
            if any(marker in upper_line for marker in _NOTICE_STOP_MARKERS):
                break
            material = self._match_notice_material(line=line, row_index=row_index)
            if material is None:
                continue
            tonnage = self._extract_notice_tonnage(line=line)
            packaging_material, subtype = self._material_mapping(material)
            material_key = (
                packaging_material if subtype is None else f"{packaging_material} {subtype}"
            )
            materials[packaging_material] = StructuredMaterialRow(
                material_key=material_key,
                packaging_material=packaging_material,
                packaging_material_subtype=subtype,
                weight_value=tonnage,
                weight_unit="tonnes" if tonnage is not None else None,
                confidence=0.95 if tonnage is not None else 0.82,
                source="auto",
                provenance={
                    "method": "notice_of_liability_line_scan",
                    "line_text": line,
                    "source_unit": "tonnes",
                    "row_index": row_index + 1,
                },
            )
            row_index += 1
        return list(materials.values())

    def _repair_notice_date(self, *, lines: list[str]) -> str | None:
        for line in lines:
            upper_line = line.upper()
            if "DATE" not in upper_line and "CAVE" not in upper_line:
                continue
            year_matches = list(re.finditer(r"(20[0-9]{2})", line))
            if not year_matches:
                continue
            year_match = year_matches[-1]
            year = year_match.group(1)
            prefix = line[: year_match.start()]
            digits = "".join(char for char in prefix if char.isdigit())
            if len(digits) >= 4:
                day_month = digits[:4]
                candidate = f"{day_month[:2]}/{day_month[2:4]}/{year}"
                normalized = normalize_date_to_iso(candidate)
                if normalized:
                    return normalized
        return None

    def _is_notice_breakdown_line(self, line: str) -> bool:
        normalized = self._normalize_letters(line)
        return (
            ("CULATION" in normalized and ("BREAKDOWN" in normalized or "BEACDOWN" in normalized))
            or ("CATEGORY" in normalized and "TONNAGE" in normalized)
        )

    def _looks_like_notice_material_row(self, line: str) -> bool:
        return (
            self._match_notice_material(
                line=line,
                row_index=0,
                allow_row_fallback=False,
            )
            is not None
        )

    def _match_notice_material(
        self,
        *,
        line: str,
        row_index: int,
        allow_row_fallback: bool = True,
    ) -> str | None:
        normalized = self._normalize_letters(line)
        if not normalized:
            return None

        best_material: str | None = None
        best_score = 0.0
        for material, variants in _NOTICE_MATERIAL_VARIANTS.items():
            for variant in variants:
                if variant in normalized:
                    return material
                score = SequenceMatcher(None, normalized[: max(len(variant), 10)], variant).ratio()
                if score > best_score:
                    best_score = score
                    best_material = material

        if best_score >= 0.58 and best_material is not None:
            return best_material
        if allow_row_fallback and row_index < len(_NOTICE_MATERIAL_ORDER):
            return _NOTICE_MATERIAL_ORDER[row_index]
        return None

    def _extract_notice_tonnage(self, *, line: str) -> Decimal | None:
        tokens = [token for token in re.split(r"\s+", line.strip()) if token]
        for token in tokens:
            candidate = self._parse_notice_numeric_token(token)
            if candidate is not None:
                return candidate
        numeric_values = _NUMERIC_PATTERN.findall(line)
        if numeric_values:
            try:
                return Decimal(numeric_values[0])
            except InvalidOperation:
                return None
        return None

    def _parse_notice_numeric_token(self, token: str) -> Decimal | None:
        stripped = token.strip()
        if not any(char.isdigit() for char in stripped):
            return None
        if stripped.startswith(_CURRENCY_PREFIXES) or stripped.upper().startswith("E"):
            return None

        if _QUOTE_ONE_PREFIX_RE.match(stripped):
            return Decimal("1.00")

        digits = re.sub(r"\D", "", stripped)
        if not digits or len(digits) > 4:
            return None
        if "." in stripped:
            try:
                return Decimal(stripped.replace(",", ""))
            except InvalidOperation:
                return None
        try:
            return Decimal(digits) / Decimal("100")
        except InvalidOperation:
            return None

    def _material_mapping(self, material: str) -> tuple[str, str | None]:
        for packaging_material, subtype in _NOTICE_MATERIAL_MAP.values():
            if packaging_material == material:
                return packaging_material, subtype
        return material, None

    def _normalize_letters(self, value: str) -> str:
        return re.sub(r"[^A-Z]", "", value.upper())

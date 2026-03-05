from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .ner_stub import NERExtractor, StubNERExtractor
from .ocr import OCRItem

INVOICE_REF_PATTERNS = [
    re.compile(
        r"\binvoice\s*(?:no|number|ref|reference)?\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]+)",
        re.IGNORECASE,
    ),
]
DATE_VALUE = (
    r"[0-9]{1,2}[./-][0-9]{1,2}[./-][0-9]{2,4}"
    r"|[0-9]{4}-[0-9]{2}-[0-9]{2}"
    r"|[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{2,4}"
)
INVOICE_DATE_PATTERNS = [
    re.compile(rf"\binvoice\s*date\s*[:#-]?\s*({DATE_VALUE})", re.IGNORECASE),
    re.compile(rf"\bdate\s*[:#-]?\s*({DATE_VALUE})", re.IGNORECASE),
]
SUPPLIER_REF_PATTERNS = [
    re.compile(
        r"\bsupplier\s*(?:ref|reference|id)\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]+)",
        re.IGNORECASE,
    )
]
SUPPLIER_NAME_PATTERNS = [
    re.compile(r"^\s*supplier\s*name\s*[:#-]?\s*(.+)$", re.IGNORECASE),
    re.compile(r"^\s*supplier\s*[:#-]?\s*(.+)$", re.IGNORECASE),
]
PRODUCT_DESC_PATTERN = re.compile(
    r"(?:product\s*(?:description|desc)?|description|item)\s*[:#-]\s*(.+)$",
    re.IGNORECASE,
)
WEIGHT_PATTERN = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>kg|g)\b", re.IGNORECASE)

REQUIRED_FIELDS = [
    "invoice_ref",
    "invoice_date",
    "supplier_ref_or_name",
    "product_desc",
    "weight_value",
    "weight_unit",
]
AMBIGUOUS_FIELDS = [
    "invoice_ref",
    "invoice_date",
    "supplier_ref",
    "supplier_name",
    "weight_value",
    "weight_unit",
]


@dataclass
class LineContext:
    text: str
    confidence: float
    page_number: int
    block_number: int
    line_number: int
    start_offset: int
    end_offset: int


@dataclass
class ExtractedCandidate:
    field_name: str
    raw_value: str
    normalized_value: str | None
    confidence: float
    source_page_number: int
    source_block_number: int | None
    source_line_number: int | None
    start_offset: int | None
    end_offset: int | None
    provenance: dict


@dataclass
class ExtractionResult:
    candidates: list[ExtractedCandidate]


class ExtractionV1Service:
    def __init__(self, *, ner_extractor: NERExtractor | None = None) -> None:
        self.ner_extractor = ner_extractor or StubNERExtractor()

    def extract_from_page(
        self,
        *,
        page_number: int,
        page_text: str,
        ocr_items: list[OCRItem],
    ) -> ExtractionResult:
        # NER interface is intentionally stubbed for future model integration.
        _ = self.ner_extractor.extract(page_text)

        line_contexts = self._build_line_contexts(
            page_number=page_number,
            page_text=page_text,
            ocr_items=ocr_items,
        )

        candidates: list[ExtractedCandidate] = []
        candidates.extend(
            self._extract_single_patterns("invoice_ref", INVOICE_REF_PATTERNS, line_contexts)
        )
        candidates.extend(
            self._extract_single_patterns("supplier_ref", SUPPLIER_REF_PATTERNS, line_contexts)
        )
        candidates.extend(
            self._extract_single_patterns("supplier_name", SUPPLIER_NAME_PATTERNS, line_contexts)
        )
        candidates.extend(self._extract_dates(line_contexts))
        candidates.extend(self._extract_product_descriptions(line_contexts))
        candidates.extend(self._extract_weights(line_contexts))

        return ExtractionResult(candidates=candidates)

    def build_review_findings(
        self, *, candidates: list[ExtractedCandidate]
    ) -> tuple[list[str], list[str]]:
        grouped = self._group_candidates(candidates)

        missing_fields: list[str] = []
        if not grouped["invoice_ref"]:
            missing_fields.append("invoice_ref")
        if not grouped["invoice_date"]:
            missing_fields.append("invoice_date")
        if not grouped["supplier_ref"] and not grouped["supplier_name"]:
            missing_fields.append("supplier_ref_or_name")
        if not grouped["product_desc"]:
            missing_fields.append("product_desc")
        if not grouped["weight_value"]:
            missing_fields.append("weight_value")
        if not grouped["weight_unit"]:
            missing_fields.append("weight_unit")

        ambiguous_fields: list[str] = []
        for field_name in AMBIGUOUS_FIELDS:
            values = {
                candidate.normalized_value or candidate.raw_value
                for candidate in grouped[field_name]
                if (candidate.normalized_value or candidate.raw_value)
            }
            if len(values) > 1:
                ambiguous_fields.append(field_name)

        if grouped["invoice_date"] and not {
            candidate.normalized_value
            for candidate in grouped["invoice_date"]
            if candidate.normalized_value
        }:
            ambiguous_fields.append("invoice_date")

        return sorted(set(missing_fields)), sorted(set(ambiguous_fields))

    def _extract_single_patterns(
        self,
        field_name: str,
        patterns: list[re.Pattern[str]],
        lines: list[LineContext],
    ) -> list[ExtractedCandidate]:
        candidates: list[ExtractedCandidate] = []
        for line in lines:
            for pattern in patterns:
                match = pattern.search(line.text)
                if not match:
                    continue
                value = match.group(1).strip()
                if not value:
                    continue
                value_start = line.start_offset + match.start(1)
                value_end = line.start_offset + match.end(1)
                candidates.append(
                    ExtractedCandidate(
                        field_name=field_name,
                        raw_value=value,
                        normalized_value=value,
                        confidence=line.confidence,
                        source_page_number=line.page_number,
                        source_block_number=line.block_number,
                        source_line_number=line.line_number,
                        start_offset=value_start,
                        end_offset=value_end,
                        provenance={
                            "method": "regex",
                            "pattern": pattern.pattern,
                            "line_text": line.text,
                        },
                    )
                )
        return candidates

    def _extract_dates(self, lines: list[LineContext]) -> list[ExtractedCandidate]:
        candidates: list[ExtractedCandidate] = []
        for line in lines:
            for pattern in INVOICE_DATE_PATTERNS:
                match = pattern.search(line.text)
                if not match:
                    continue
                raw_value = match.group(1).strip()
                normalized = normalize_date_to_iso(raw_value)
                candidates.append(
                    ExtractedCandidate(
                        field_name="invoice_date",
                        raw_value=raw_value,
                        normalized_value=normalized,
                        confidence=line.confidence,
                        source_page_number=line.page_number,
                        source_block_number=line.block_number,
                        source_line_number=line.line_number,
                        start_offset=line.start_offset + match.start(1),
                        end_offset=line.start_offset + match.end(1),
                        provenance={
                            "method": "regex",
                            "pattern": pattern.pattern,
                            "line_text": line.text,
                        },
                    )
                )
        return candidates

    def _extract_product_descriptions(self, lines: list[LineContext]) -> list[ExtractedCandidate]:
        candidates: list[ExtractedCandidate] = []
        for line in lines:
            match = PRODUCT_DESC_PATTERN.search(line.text)
            if not match:
                continue
            raw_value = match.group(1).strip()
            if not raw_value:
                continue
            candidates.append(
                ExtractedCandidate(
                    field_name="product_desc",
                    raw_value=raw_value,
                    normalized_value=raw_value,
                    confidence=line.confidence,
                    source_page_number=line.page_number,
                    source_block_number=line.block_number,
                    source_line_number=line.line_number,
                    start_offset=line.start_offset + match.start(1),
                    end_offset=line.start_offset + match.end(1),
                    provenance={
                        "method": "regex",
                        "pattern": PRODUCT_DESC_PATTERN.pattern,
                        "line_text": line.text,
                    },
                )
            )
        return candidates

    def _extract_weights(self, lines: list[LineContext]) -> list[ExtractedCandidate]:
        candidates: list[ExtractedCandidate] = []
        for line in lines:
            for match in WEIGHT_PATTERN.finditer(line.text):
                value_raw = match.group("value")
                unit_raw = match.group("unit")
                normalized_weight = normalize_weight_to_kg(value_raw, unit_raw)
                if normalized_weight is None:
                    continue

                value_start = line.start_offset + match.start("value")
                value_end = line.start_offset + match.end("value")
                unit_start = line.start_offset + match.start("unit")
                unit_end = line.start_offset + match.end("unit")

                candidates.append(
                    ExtractedCandidate(
                        field_name="weight_value",
                        raw_value=value_raw,
                        normalized_value=f"{normalized_weight:.6f}",
                        confidence=line.confidence,
                        source_page_number=line.page_number,
                        source_block_number=line.block_number,
                        source_line_number=line.line_number,
                        start_offset=value_start,
                        end_offset=value_end,
                        provenance={
                            "method": "regex",
                            "pattern": WEIGHT_PATTERN.pattern,
                            "line_text": line.text,
                            "source_unit": unit_raw,
                        },
                    )
                )
                candidates.append(
                    ExtractedCandidate(
                        field_name="weight_unit",
                        raw_value=unit_raw,
                        normalized_value="kg",
                        confidence=line.confidence,
                        source_page_number=line.page_number,
                        source_block_number=line.block_number,
                        source_line_number=line.line_number,
                        start_offset=unit_start,
                        end_offset=unit_end,
                        provenance={
                            "method": "regex",
                            "pattern": WEIGHT_PATTERN.pattern,
                            "line_text": line.text,
                            "source_unit": unit_raw,
                        },
                    )
                )
        return candidates

    def _build_line_contexts(
        self,
        *,
        page_number: int,
        page_text: str,
        ocr_items: list[OCRItem],
    ) -> list[LineContext]:
        line_items = [item for item in ocr_items if item.item_type == "line"]
        if not line_items:
            stripped_lines = [line.strip() for line in page_text.splitlines() if line.strip()]
            cursor = 0
            contexts: list[LineContext] = []
            for index, line_text in enumerate(stripped_lines, start=1):
                position = page_text.find(line_text, cursor)
                start = position if position >= 0 else cursor
                end = start + len(line_text)
                cursor = end
                contexts.append(
                    LineContext(
                        text=line_text,
                        confidence=0.5,
                        page_number=page_number,
                        block_number=index,
                        line_number=index,
                        start_offset=start,
                        end_offset=end,
                    )
                )
            return contexts

        sorted_lines = sorted(line_items, key=lambda item: (item.block_number, item.line_number))
        contexts: list[LineContext] = []
        cursor = 0
        for line in sorted_lines:
            position = page_text.find(line.text, cursor)
            if position < 0:
                position = page_text.find(line.text)
            if position < 0:
                position = cursor
            start = position
            end = start + len(line.text)
            cursor = end
            contexts.append(
                LineContext(
                    text=line.text,
                    confidence=line.confidence,
                    page_number=page_number,
                    block_number=line.block_number,
                    line_number=line.line_number,
                    start_offset=start,
                    end_offset=end,
                )
            )
        return contexts

    @staticmethod
    def _group_candidates(
        candidates: list[ExtractedCandidate],
    ) -> dict[str, list[ExtractedCandidate]]:
        grouped: dict[str, list[ExtractedCandidate]] = {
            field: [] for field in REQUIRED_FIELDS + AMBIGUOUS_FIELDS
        }
        for candidate in candidates:
            grouped.setdefault(candidate.field_name, []).append(candidate)
        return grouped


def normalize_date_to_iso(raw_value: str) -> str | None:
    clean = raw_value.strip()
    clean = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", clean, flags=re.IGNORECASE)

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d %b %Y",
        "%d %B %Y",
        "%d %b %y",
        "%d %B %y",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(clean, fmt)
            return parsed.date().isoformat()
        except ValueError:
            continue
    return None


def normalize_weight_to_kg(raw_value: str, raw_unit: str) -> float | None:
    try:
        numeric = float(raw_value.replace(",", ""))
    except ValueError:
        return None

    unit = raw_unit.strip().lower()
    if unit == "kg":
        return numeric
    if unit == "g":
        return numeric / 1000.0
    return None

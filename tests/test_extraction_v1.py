from __future__ import annotations

from api.app.services.extraction_v1 import (
    ExtractionV1Service,
    normalize_date_to_iso,
    normalize_weight_to_kg,
)
from api.app.services.ocr import OCRItem


def test_normalize_date_to_iso_fixtures() -> None:
    fixtures = {
        "2026-03-04": "2026-03-04",
        "04/03/2026": "2026-03-04",
        "04-03-2026": "2026-03-04",
        "4 Mar 2026": "2026-03-04",
        "4 March 2026": "2026-03-04",
        "4th March 2026": "2026-03-04",
        "not-a-date": None,
    }

    for raw, expected in fixtures.items():
        assert normalize_date_to_iso(raw) == expected


def test_normalize_weight_to_kg_fixtures() -> None:
    assert normalize_weight_to_kg("1500", "g") == 1.5
    assert normalize_weight_to_kg("1.75", "kg") == 1.75
    assert normalize_weight_to_kg("2,500", "g") == 2.5
    assert normalize_weight_to_kg("bad", "kg") is None
    assert normalize_weight_to_kg("10", "lb") is None


def test_extraction_parser_extracts_required_fields_with_provenance() -> None:
    page_text = (
        "Invoice Number: INV-7788\n"
        "Invoice Date: 04/03/2026\n"
        "Supplier Name: Acme Components Ltd\n"
        "Supplier Ref: SUP-991\n"
        "Product Description: Industrial adhesive cartridge\n"
        "Weight: 1500 g\n"
    )

    lines = [
        "Invoice Number: INV-7788",
        "Invoice Date: 04/03/2026",
        "Supplier Name: Acme Components Ltd",
        "Supplier Ref: SUP-991",
        "Product Description: Industrial adhesive cartridge",
        "Weight: 1500 g",
    ]

    ocr_items = [
        OCRItem(
            item_type="line",
            text=line,
            confidence=0.92,
            bbox={"left": 0, "top": 0, "width": 100, "height": 10},
            page_number=1,
            block_number=index,
            line_number=index,
            token_number=0,
        )
        for index, line in enumerate(lines, start=1)
    ]

    service = ExtractionV1Service()
    result = service.extract_from_page(page_number=1, page_text=page_text, ocr_items=ocr_items)

    fields = {candidate.field_name for candidate in result.candidates}
    assert "invoice_ref" in fields
    assert "invoice_date" in fields
    assert "supplier_name" in fields
    assert "supplier_ref" in fields
    assert "product_desc" in fields
    assert "weight_value" in fields
    assert "weight_unit" in fields

    invoice_date = next(
        candidate for candidate in result.candidates if candidate.field_name == "invoice_date"
    )
    assert invoice_date.normalized_value == "2026-03-04"
    assert invoice_date.start_offset is not None
    assert invoice_date.end_offset is not None
    assert invoice_date.provenance["method"] == "regex"

    weight_value = next(
        candidate for candidate in result.candidates if candidate.field_name == "weight_value"
    )
    weight_unit = next(
        candidate for candidate in result.candidates if candidate.field_name == "weight_unit"
    )
    assert weight_value.normalized_value == "1.500000"
    assert weight_unit.normalized_value == "kg"


def test_extraction_review_findings_for_missing_and_ambiguous_fields() -> None:
    page_text = (
        "Invoice Number: INV-1\nInvoice Number: INV-2\nProduct Description: Item A\nWeight: 1 kg\n"
    )
    ocr_items = [
        OCRItem(
            item_type="line",
            text="Invoice Number: INV-1",
            confidence=0.9,
            bbox={"left": 0, "top": 0, "width": 100, "height": 10},
            page_number=1,
            block_number=1,
            line_number=1,
            token_number=0,
        ),
        OCRItem(
            item_type="line",
            text="Invoice Number: INV-2",
            confidence=0.9,
            bbox={"left": 0, "top": 0, "width": 100, "height": 10},
            page_number=1,
            block_number=2,
            line_number=2,
            token_number=0,
        ),
        OCRItem(
            item_type="line",
            text="Product Description: Item A",
            confidence=0.9,
            bbox={"left": 0, "top": 0, "width": 100, "height": 10},
            page_number=1,
            block_number=3,
            line_number=3,
            token_number=0,
        ),
        OCRItem(
            item_type="line",
            text="Weight: 1 kg",
            confidence=0.9,
            bbox={"left": 0, "top": 0, "width": 100, "height": 10},
            page_number=1,
            block_number=4,
            line_number=4,
            token_number=0,
        ),
    ]

    service = ExtractionV1Service()
    result = service.extract_from_page(page_number=1, page_text=page_text, ocr_items=ocr_items)
    missing, ambiguous = service.build_review_findings(candidates=result.candidates)

    assert "invoice_date" in missing
    assert "supplier_ref_or_name" in missing
    assert "invoice_ref" in ambiguous

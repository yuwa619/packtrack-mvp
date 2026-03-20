from __future__ import annotations

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "index.json"


FIXTURES = [
    {
        "pdf_file": "invoice_table_top_left.pdf",
        "layout": "table",
        "supplier_name": "North PET Packaging Ltd",
        "invoice_ref": "INVPET1001",
        "invoice_date": "01/03/2026",
        "line_items": [
            "Item: PET plastic bottle sleeves - 1200 g",
            "Item: Cardboard transit cartons - 0.8 kg",
            "Item: PET label rolls - 300 g",
        ],
        "packaging_hints": "brand household primary PET plastic cardboard",
        "expected_extracted_fields": {
            "invoice_ref": "INVPET1001",
            "invoice_date": "2026-03-01",
            "supplier_name": "North PET Packaging Ltd",
            "stable_ocr_token": "North",
            "required_fields": [
                "invoice_ref",
                "invoice_date",
                "supplier_name",
                "product_desc",
                "weight_value",
                "weight_unit",
            ],
        },
        "expected_taxonomy_codes": ["Plastic"],
    },
    {
        "pdf_file": "invoice_right_header_glass.pdf",
        "layout": "right_header",
        "supplier_name": "Glassworks UK Ltd",
        "invoice_ref": "GL2042",
        "invoice_date": "2026-03-02",
        "line_items": [
            "Item: Glass jars for sauces - 2.5 kg",
            "Item: Glass bottle necks - 750 g",
        ],
        "packaging_hints": "brand household primary glass",
        "expected_extracted_fields": {
            "invoice_ref": "GL2042",
            "invoice_date": "2026-03-02",
            "supplier_name": "Glassworks UK Ltd",
            "stable_ocr_token": "Glassworks",
            "required_fields": [
                "invoice_ref",
                "invoice_date",
                "supplier_name",
                "product_desc",
                "weight_value",
                "weight_unit",
            ],
        },
        "expected_taxonomy_codes": ["Glass"],
    },
    {
        "pdf_file": "invoice_lines_aluminium.pdf",
        "layout": "lines",
        "supplier_name": "AluCan Imports",
        "invoice_ref": "ALU7788",
        "invoice_date": "03-03-2026",
        "line_items": [
            "Item: Aluminium can bodies - 750 g",
            "Item: Aluminium can lids - 0.4 kg",
            "Item: Aluminium foil wraps - 300 g",
        ],
        "packaging_hints": "imported non-household secondary aluminium",
        "expected_extracted_fields": {
            "invoice_ref": "ALU7788",
            "invoice_date": "2026-03-03",
            "supplier_name": "AluCan Imports",
            "stable_ocr_token": "AluCan",
            "required_fields": [
                "invoice_ref",
                "invoice_date",
                "supplier_name",
                "product_desc",
                "weight_value",
                "weight_unit",
            ],
        },
        "expected_taxonomy_codes": ["Aluminium"],
    },
    {
        "pdf_file": "invoice_two_column_cardboard.pdf",
        "layout": "two_column",
        "supplier_name": "Fibre Cardboard Co",
        "invoice_ref": "CB5501",
        "invoice_date": "04 March 2026",
        "line_items": [
            "Item: Cardboard trays - 2.0 kg",
            "Item: Cardboard sleeve packs - 650 g",
            "Item: Cardboard inserts - 500 g",
            "Item: Cardboard edge guards - 0.3 kg",
        ],
        "packaging_hints": "brand household secondary cardboard",
        "expected_extracted_fields": {
            "invoice_ref": "CB5501",
            "invoice_date": "2026-03-04",
            "supplier_name": "Fibre Cardboard Co",
            "stable_ocr_token": "Fibre",
            "required_fields": [
                "invoice_ref",
                "invoice_date",
                "supplier_name",
                "product_desc",
                "weight_value",
                "weight_unit",
            ],
        },
        "expected_taxonomy_codes": ["Paper or cardboard"],
    },
]


def _content_lines(item: dict) -> list[str]:
    lines: list[str] = [
        f"Supplier name: {item['supplier_name']}",
        f"Invoice ref: {item['invoice_ref']}",
        f"Invoice date: {item['invoice_date']}",
    ]
    lines.extend(item["line_items"])
    lines.append(f"Packaging notes: {item['packaging_hints']}")
    return lines


def _render_fallback_image(item: dict) -> str:
    png_name = item["pdf_file"].replace(".pdf", ".png")
    png_path = ROOT / png_name

    image = np.full((2200, 1700, 3), 255, dtype=np.uint8)

    layout = item["layout"]
    lines = _content_lines(item)

    def draw_text(*, x: int, y: int, text: str, scale: float = 1.2, thickness: int = 2) -> None:
        cv2.putText(
            image,
            text,
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            (0, 0, 0),
            thickness,
            cv2.LINE_8,
        )

    if layout == "table":
        draw_text(x=80, y=100, text="Invoice", scale=1.4, thickness=3)
        y = 200
        for line in lines:
            draw_text(x=80, y=y, text=line)
            y += 90
    elif layout == "right_header":
        draw_text(x=80, y=100, text="Invoice", scale=1.4, thickness=3)
        y_header = 200
        for line in lines[:3]:
            draw_text(x=760, y=y_header, text=line, scale=1.0)
            y_header += 90
        y = 500
        for line in lines[3:]:
            draw_text(x=80, y=y, text=line)
            y += 90
    elif layout == "lines":
        draw_text(x=80, y=100, text="Invoice", scale=1.4, thickness=3)
        y = 210
        for line in lines:
            draw_text(x=80, y=y, text=line)
            y += 84
    elif layout == "two_column":
        draw_text(x=80, y=100, text="Invoice", scale=1.4, thickness=3)
        draw_text(x=80, y=210, text=lines[0], scale=1.05)
        draw_text(x=780, y=210, text=lines[1], scale=1.0)
        draw_text(x=780, y=300, text=lines[2], scale=1.0)
        y_left = 420
        y_right = 420
        for idx, line in enumerate(lines[3:]):
            if idx % 2 == 0:
                draw_text(x=80, y=y_left, text=line, scale=1.0)
                y_left += 90
            else:
                draw_text(x=780, y=y_right, text=line, scale=1.0)
                y_right += 90
    else:
        raise ValueError(f"Unsupported layout: {layout}")

    cv2.imwrite(str(png_path), image)
    return png_name


def _draw_common(c: canvas.Canvas, item: dict) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 810, "Invoice")
    c.setFont("Helvetica", 10)
    c.drawString(40, 794, f"Supplier name: {item['supplier_name']}")
    c.drawString(40, 780, f"Invoice ref: {item['invoice_ref']}")
    c.drawString(40, 766, f"Invoice date: {item['invoice_date']}")


def _draw_table_layout(c: canvas.Canvas, item: dict) -> None:
    _draw_common(c, item)
    top_y = 730
    c.setFont("Helvetica-Bold", 10)
    c.drawString(45, top_y, "Line")
    c.drawString(85, top_y, "Description")
    c.drawString(470, top_y, "Weight")
    c.line(40, top_y - 4, 550, top_y - 4)

    c.setFont("Helvetica", 10)
    y = top_y - 22
    for idx, line in enumerate(item["line_items"], start=1):
        c.drawString(45, y, str(idx))
        c.drawString(85, y, line)
        c.line(40, y - 6, 550, y - 6)
        y -= 22

    c.setFont("Helvetica", 10)
    c.drawString(40, y - 8, f"Packaging notes: {item['packaging_hints']}")


def _draw_right_header_layout(c: canvas.Canvas, item: dict) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 810, "Invoice")

    c.setFont("Helvetica", 10)
    c.drawString(300, 810, f"Supplier name: {item['supplier_name']}")
    c.drawString(300, 796, f"Invoice ref: {item['invoice_ref']}")
    c.drawString(300, 782, f"Invoice date: {item['invoice_date']}")

    y = 740
    for line in item["line_items"]:
        c.drawString(40, y, line)
        y -= 20

    c.drawString(40, y - 8, f"Packaging notes: {item['packaging_hints']}")


def _draw_lines_layout(c: canvas.Canvas, item: dict) -> None:
    _draw_common(c, item)
    c.setFont("Helvetica", 10)
    y = 730
    for line in item["line_items"]:
        c.drawString(40, y, line)
        y -= 18

    c.drawString(40, y - 6, f"Packaging notes: {item['packaging_hints']}")


def _draw_two_column_layout(c: canvas.Canvas, item: dict) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 810, "Invoice")

    c.setFont("Helvetica", 10)
    c.drawString(40, 794, f"Supplier name: {item['supplier_name']}")
    c.drawString(300, 794, f"Invoice ref: {item['invoice_ref']}")
    c.drawString(300, 780, f"Invoice date: {item['invoice_date']}")

    y_left = 740
    y_right = 740
    items = item["line_items"]
    for idx, line in enumerate(items):
        if idx % 2 == 0:
            c.drawString(40, y_left, line)
            y_left -= 20
        else:
            c.drawString(300, y_right, line)
            y_right -= 20

    c.drawString(40, min(y_left, y_right) - 8, f"Packaging notes: {item['packaging_hints']}")


def _render_fixture(item: dict) -> None:
    pdf_path = ROOT / item["pdf_file"]
    c = canvas.Canvas(str(pdf_path), pagesize=A4)

    layout = item["layout"]
    if layout == "table":
        _draw_table_layout(c, item)
    elif layout == "right_header":
        _draw_right_header_layout(c, item)
    elif layout == "lines":
        _draw_lines_layout(c, item)
    elif layout == "two_column":
        _draw_two_column_layout(c, item)
    else:
        raise ValueError(f"Unsupported layout: {layout}")

    c.showPage()
    c.save()


def main() -> None:
    output = {"fixtures": []}
    for item in FIXTURES:
        _render_fixture(item)
        fallback_image_file = _render_fallback_image(item)
        pdf_path = ROOT / item["pdf_file"]
        pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        output["fixtures"].append(
            {
                "pdf_file": item["pdf_file"],
                "layout": item["layout"],
                "pdf_sha256": pdf_sha256,
                "fallback_image_file": fallback_image_file,
                "expected_extracted_fields": item["expected_extracted_fields"],
                "expected_taxonomy_codes": item["expected_taxonomy_codes"],
            }
        )

    INDEX_PATH.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {INDEX_PATH}")
    for item in FIXTURES:
        print(f"Wrote {ROOT / item['pdf_file']}")


if __name__ == "__main__":
    main()

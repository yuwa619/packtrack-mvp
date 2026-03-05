from __future__ import annotations


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _render_content_stream(lines: list[str]) -> str:
    commands = ["BT", "/F1 12 Tf", "72 740 Td"]
    if lines:
        commands.append(f"({_escape_pdf_text(lines[0])}) Tj")
    for line in lines[1:]:
        commands.append("0 -18 Td")
        commands.append(f"({_escape_pdf_text(line)}) Tj")
    commands.append("ET")
    return "\n".join(commands) + "\n"


def generate_demo_invoice_pdf() -> bytes:
    lines = [
        "PackTrack Demo Invoice",
        "",
        "Supplier Name: Acme Packaging Ltd",
        "Invoice Ref: INV-PT-001",
        "Invoice Date: 2025-01-15",
        "Product Description: Household rigid plastic bottle with cardboard sleeve",
        "Packaging Material Hint: Plastic and Paper or cardboard",
        "Weight: 1200 g",
    ]
    stream = _render_content_stream(lines)
    stream_bytes = stream.encode("utf-8")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Length "
            + str(len(stream_bytes)).encode("ascii")
            + b" >>\nstream\n"
            + stream_bytes
            + b"endstream"
        ),
    ]

    pdf = bytearray()
    pdf.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))

    pdf.extend(
        (
            "trailer\n"
            f"<< /Size {len(offsets)} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_offset}\n"
            "%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from ..config import settings
from .storage import ObjectStorage


@dataclass
class OCRItem:
    item_type: str
    text: str
    confidence: float
    bbox: dict[str, int]
    page_number: int
    block_number: int
    line_number: int
    token_number: int


@dataclass
class OCRPageResult:
    page_number: int
    raw_text: str
    tsv_text: str
    hocr_bytes: bytes
    items: list[OCRItem]
    artifact_text_uri: str
    artifact_tsv_uri: str
    artifact_hocr_uri: str


class OCRService:
    def __init__(self, *, storage: ObjectStorage) -> None:
        self.storage = storage

    def process_page(self, *, document_id, page_number: int, image_uri: str) -> OCRPageResult:
        bucket, key = self.storage.parse_uri(image_uri)
        image_bytes = self.storage.get_bytes(bucket=bucket, key=key)

        np_image = np.frombuffer(image_bytes, dtype=np.uint8)
        image = cv2.imdecode(np_image, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ValueError(f"Unable to decode page image: {image_uri}")

        raw_text = pytesseract.image_to_string(image, config="--psm 6")
        tsv_text = pytesseract.image_to_data(image, config="--psm 6")
        tsv_dict = pytesseract.image_to_data(image, config="--psm 6", output_type=Output.DICT)
        hocr_bytes = pytesseract.image_to_pdf_or_hocr(image, extension="hocr", config="--psm 6")

        items = self._extract_items(tsv_dict)

        text_uri = self.storage.put_bytes(
            bucket=settings.minio_bucket_raw,
            key=f"ocr-output/{document_id}/page-{page_number}.txt",
            data=raw_text.encode("utf-8"),
            content_type="text/plain",
        )
        tsv_uri = self.storage.put_bytes(
            bucket=settings.minio_bucket_raw,
            key=f"ocr-output/{document_id}/page-{page_number}.tsv",
            data=tsv_text.encode("utf-8"),
            content_type="text/tab-separated-values",
        )
        hocr_uri = self.storage.put_bytes(
            bucket=settings.minio_bucket_raw,
            key=f"ocr-output/{document_id}/page-{page_number}.hocr",
            data=hocr_bytes,
            content_type="text/html",
        )

        return OCRPageResult(
            page_number=page_number,
            raw_text=raw_text,
            tsv_text=tsv_text,
            hocr_bytes=hocr_bytes,
            items=items,
            artifact_text_uri=text_uri,
            artifact_tsv_uri=tsv_uri,
            artifact_hocr_uri=hocr_uri,
        )

    def _extract_items(self, tsv_dict: dict[str, list]) -> list[OCRItem]:
        tokens: list[OCRItem] = []
        token_rows: list[dict[str, int | str | float]] = []

        row_count = len(tsv_dict.get("text", []))
        for idx in range(row_count):
            text = str(tsv_dict["text"][idx]).strip()
            conf_raw = str(tsv_dict["conf"][idx]).strip()
            if not text or conf_raw in {"-1", ""}:
                continue
            try:
                confidence = float(conf_raw) / 100.0
            except ValueError:
                continue

            row = {
                "page_num": int(tsv_dict["page_num"][idx]),
                "block_num": int(tsv_dict["block_num"][idx]),
                "line_num": int(tsv_dict["line_num"][idx]),
                "par_num": int(tsv_dict["par_num"][idx]),
                "word_num": int(tsv_dict["word_num"][idx]),
                "left": int(tsv_dict["left"][idx]),
                "top": int(tsv_dict["top"][idx]),
                "width": int(tsv_dict["width"][idx]),
                "height": int(tsv_dict["height"][idx]),
                "text": text,
                "confidence": confidence,
            }
            token_rows.append(row)

            tokens.append(
                OCRItem(
                    item_type="token",
                    text=text,
                    confidence=confidence,
                    bbox={
                        "left": row["left"],
                        "top": row["top"],
                        "width": row["width"],
                        "height": row["height"],
                    },
                    page_number=row["page_num"],
                    block_number=row["block_num"],
                    line_number=row["line_num"],
                    token_number=row["word_num"],
                )
            )

        lines = self._aggregate_rows(token_rows=token_rows, item_type="line")
        blocks = self._aggregate_rows(token_rows=token_rows, item_type="block")
        return blocks + lines + tokens

    def _aggregate_rows(
        self, *, token_rows: list[dict[str, int | str | float]], item_type: str
    ) -> list[OCRItem]:
        grouped: dict[tuple[int, int, int], list[dict[str, int | str | float]]] = {}
        for row in token_rows:
            if item_type == "line":
                key = (int(row["page_num"]), int(row["block_num"]), int(row["line_num"]))
            else:
                key = (int(row["page_num"]), int(row["block_num"]), 0)
            grouped.setdefault(key, []).append(row)

        items: list[OCRItem] = []
        for (page_num, block_num, line_num), rows in grouped.items():
            text = " ".join(str(entry["text"]) for entry in rows)
            confidence = sum(float(entry["confidence"]) for entry in rows) / len(rows)

            left = min(int(entry["left"]) for entry in rows)
            top = min(int(entry["top"]) for entry in rows)
            right = max(int(entry["left"]) + int(entry["width"]) for entry in rows)
            bottom = max(int(entry["top"]) + int(entry["height"]) for entry in rows)

            items.append(
                OCRItem(
                    item_type=item_type,
                    text=text,
                    confidence=confidence,
                    bbox={
                        "left": left,
                        "top": top,
                        "width": right - left,
                        "height": bottom - top,
                    },
                    page_number=page_num,
                    block_number=block_num,
                    line_number=line_num,
                    token_number=0,
                )
            )
        return items

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import cv2
import numpy as np
from pdf2image import convert_from_bytes

from ..config import settings
from .storage import ObjectStorage

logger = logging.getLogger(__name__)


@dataclass
class PreprocessedPage:
    page_number: int
    width: int
    height: int
    raw_image_uri: str
    normalised_image_uri: str
    processing_ms: float


class PreprocessService:
    def __init__(self, *, storage: ObjectStorage) -> None:
        self.storage = storage

    def preprocess_document(self, *, document) -> list[PreprocessedPage]:
        bucket, key = self.storage.parse_uri(document.storage_path)
        source_bytes = self.storage.get_bytes(bucket=bucket, key=key)

        page_images = self._to_page_images(
            payload=source_bytes,
            mime_type=document.mime_type,
        )

        results: list[PreprocessedPage] = []
        for page_number, image in enumerate(page_images, start=1):
            start = time.perf_counter()

            raw_png = self._encode_png(image)
            raw_uri = self.storage.put_bytes(
                bucket=settings.minio_bucket_raw,
                key=f"pages/raw/{document.id}/page-{page_number}.png",
                data=raw_png,
                content_type="image/png",
            )

            normalised_image = self._normalise_image(image)
            normalised_png = self._encode_png(normalised_image)
            normalised_uri = self.storage.put_bytes(
                bucket=settings.minio_bucket_preprocessed,
                key=f"pages/normalised/{document.id}/page-{page_number}.png",
                data=normalised_png,
                content_type="image/png",
            )

            height, width = normalised_image.shape[:2]
            processing_ms = (time.perf_counter() - start) * 1000.0

            logger.info(
                "preprocess.page document_id=%s page=%s width=%s height=%s processing_ms=%.2f",
                document.id,
                page_number,
                width,
                height,
                processing_ms,
            )

            results.append(
                PreprocessedPage(
                    page_number=page_number,
                    width=width,
                    height=height,
                    raw_image_uri=raw_uri,
                    normalised_image_uri=normalised_uri,
                    processing_ms=processing_ms,
                )
            )

        return results

    def _to_page_images(self, *, payload: bytes, mime_type: str) -> list[np.ndarray]:
        if mime_type == "application/pdf":
            pil_images = convert_from_bytes(payload, dpi=200, fmt="png")
            return [cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR) for image in pil_images]

        image_array = np.frombuffer(payload, dtype=np.uint8)
        decoded = cv2.imdecode(image_array, cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError("Unable to decode image payload")
        return [decoded]

    def _normalise_image(self, image: np.ndarray) -> np.ndarray:
        grayscale = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        otsu_threshold = cv2.threshold(
            grayscale,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )[1]
        adaptive_threshold = cv2.adaptiveThreshold(
            grayscale,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            35,
            11,
        )

        deskew_base = self._choose_best_binary(otsu_threshold, adaptive_threshold)
        angle = self._estimate_skew_angle(deskew_base)
        deskewed = self._rotate(grayscale, -angle)

        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        contrast = clahe.apply(deskewed)
        denoised = cv2.medianBlur(contrast, 3)

        final_otsu = cv2.threshold(
            denoised,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )[1]
        final_adaptive = cv2.adaptiveThreshold(
            denoised,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            35,
            11,
        )
        return self._choose_best_binary(final_otsu, final_adaptive)

    @staticmethod
    def _estimate_skew_angle(binary_image: np.ndarray) -> float:
        edges = cv2.Canny(binary_image, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(
            image=edges,
            rho=1,
            theta=np.pi / 180,
            threshold=100,
            minLineLength=100,
            maxLineGap=10,
        )
        if lines is None:
            return 0.0

        angles: list[float] = []
        for line in lines[:, 0]:
            x1, y1, x2, y2 = line
            angle = float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
            if -45.0 <= angle <= 45.0:
                angles.append(angle)

        if not angles:
            return 0.0

        median_angle = float(np.median(angles))
        return float(np.clip(median_angle, -15.0, 15.0))

    @staticmethod
    def _rotate(image: np.ndarray, angle_degrees: float) -> np.ndarray:
        if abs(angle_degrees) < 0.01:
            return image
        height, width = image.shape[:2]
        center = (width / 2.0, height / 2.0)
        matrix = cv2.getRotationMatrix2D(center, angle_degrees, 1.0)
        return cv2.warpAffine(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )

    @staticmethod
    def _choose_best_binary(first: np.ndarray, second: np.ndarray) -> np.ndarray:
        def score(binary: np.ndarray) -> float:
            foreground_ratio = float(np.count_nonzero(binary == 0)) / float(binary.size)
            return abs(foreground_ratio - 0.5)

        return first if score(first) <= score(second) else second

    @staticmethod
    def _encode_png(image: np.ndarray) -> bytes:
        success, buffer = cv2.imencode(".png", image)
        if not success:
            raise ValueError("Failed to encode PNG")
        return buffer.tobytes()

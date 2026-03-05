from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class NERPrediction:
    label: str
    text: str
    confidence: float
    start_offset: int
    end_offset: int


class NERExtractor(Protocol):
    def extract(self, text: str) -> list[NERPrediction]:
        """Return NER predictions for the text."""


class StubNERExtractor:
    # TODO: replace with spaCy-based extractor when training/inference is introduced.
    def extract(self, text: str) -> list[NERPrediction]:
        return []

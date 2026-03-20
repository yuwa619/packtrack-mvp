from __future__ import annotations

from pathlib import Path

from .ner_stub import NERPrediction


class SpacyNERExtractor:
    def __init__(self, *, model_path: str) -> None:
        import spacy

        resolved_path = Path(model_path)
        if not resolved_path.exists():
            raise FileNotFoundError(f"spaCy NER model path not found: {resolved_path}")
        self.nlp = spacy.load(resolved_path)

    def extract(self, text: str) -> list[NERPrediction]:
        doc = self.nlp(text)
        predictions: list[NERPrediction] = []
        for entity in doc.ents:
            confidence_raw = getattr(entity._, "confidence", 0.9)
            try:
                confidence = float(confidence_raw)
            except (TypeError, ValueError):
                confidence = 0.9
            confidence = max(0.0, min(1.0, confidence))
            predictions.append(
                NERPrediction(
                    label=entity.label_,
                    text=entity.text,
                    confidence=confidence,
                    start_offset=entity.start_char,
                    end_offset=entity.end_char,
                )
            )
        return predictions

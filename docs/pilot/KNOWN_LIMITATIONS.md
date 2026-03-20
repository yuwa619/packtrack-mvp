# Known Limitations

## MVP-Level Components
- Extraction is rule/regex-based and may miss unusual invoice layouts.
- NER is currently a stubbed interface (no trained model in pilot).
- Classification is deterministic rules + taxonomy lookup, not model-trained classification.

## Scope Exclusions
- Direct RPD/DEFRA API submission (Phase 2) is not implemented.
- No dedicated self-service deletion endpoint.

## Input Constraints
- Unsupported formats are rejected.
- Very poor quality scans, heavily skewed documents, or physically damaged originals may produce weak OCR/confidence.

## Performance Expectations
- Pipeline execution is synchronous via `POST /api/v1/pipeline/run/{document_id}`.
- Processing time depends on document quality and page count.
- Recommended operating pattern:
  - Start with batches of 10-20 documents.
  - Increase batch size only when review queue and support load remain stable.

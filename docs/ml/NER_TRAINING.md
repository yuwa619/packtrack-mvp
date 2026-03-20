# NER Training (Local Pilot)

This guide covers local training of the PackTrack invoice NER model from Label Studio exports.

## 1) Export from Label Studio

1. Open your local Label Studio project.
2. Export annotations as JSON (`Export > JSON`).
3. Save the export under either:
   - `data/labels/`, or
   - `docs/labelstudio/` (including checkpoint folders).

If `--input` is not provided, the converter picks the most recently modified export JSON found in those paths.

## 2) Convert export to spaCy DocBin

From repository root:

```bash
python -m api.scripts.labelstudio_to_spacy \
  --input docs/labelstudio/sample_export.json \
  --output-dir data/training/spacy
```

Outputs:
- `data/training/spacy/train.spacy`
- `data/training/spacy/dev.spacy`
- `data/training/spacy/labels.json`

Notes:
- Split is 80/20 train/dev, stratified by label where possible.
- Spans are validated and repaired using substring search where needed.
- Invalid spans that cannot be repaired are dropped and reported by reason.

## 3) Train the model

```bash
python -m api.scripts.train_spacy_ner \
  --config api/training/spacy_config.cfg \
  --train-data data/training/spacy/train.spacy \
  --dev-data data/training/spacy/dev.spacy \
  --output-root data/models/spacy_ner
```

Model outputs are written to:
- `data/models/spacy_ner/<timestamp>/model-best` (preferred)
- `data/models/spacy_ner/<timestamp>/model-last`
- `data/models/spacy_ner/<timestamp>/metrics.json`

The default config is `tok2vec` for lower local runtime cost.

## 4) Enable model locally

Set environment variables (for local runs only):

```bash
NER_ENABLED=true
NER_MODEL_PATH=data/models/spacy_ner/<timestamp>/model-best
```

When `NER_ENABLED=false` (default), extraction keeps the current heuristic path.

## 5) Evaluate and compare runs

Use `metrics.json` in each run folder to compare:
- overall precision/recall/F1
- per-label precision/recall/F1

Suggested local comparison workflow:
1. Keep each run timestamped under `data/models/spacy_ner/`.
2. Compare `metrics.json` across runs.
3. Promote the best model by setting `NER_MODEL_PATH` to that run’s `model-best`.

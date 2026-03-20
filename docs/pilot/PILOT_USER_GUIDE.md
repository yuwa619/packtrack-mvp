# Pilot User Guide

## Step 1: Upload Documents
- UI: use Upload screen.
- Upload modes:
  - Multiple files: select one or more PDFs/images and use `Upload + Run Pipeline (Batch)`.
  - ZIP batch: switch to `ZIP batch`, choose one `.zip`, and use `Upload ZIP + Process Batch`.
- API:
  1. Single document:
     - `POST /api/v1/documents/upload/presign`
     - Upload bytes to `upload_url`
     - `POST /api/v1/documents/upload/finalise`
  2. Multi-file batch:
     - `POST /api/v1/batches`
     - Upload each file to its `upload_url`
     - `POST /api/v1/batches/{batch_id}/finalise`
  3. ZIP batch:
     - `POST /api/v1/batches/upload-zip/presign`
     - Upload ZIP bytes to `upload_url`
     - `POST /api/v1/batches/{batch_id}/finalise-zip`

Single-document `finalise` returns `document_id` and `job_id`.
ZIP finalise returns accepted/rejected file lists plus accepted `document_id` values.

## Step 2: Run Pipeline
- API: `POST /api/v1/pipeline/run/{document_id}`
- Batch API: `POST /api/v1/batches/{batch_id}/run`
- Optional idempotency header: `Idempotency-Key`

Pipeline states:
- `QUEUED -> PREPROCESSING -> EXTRACTING -> CLASSIFYING -> REPORTING -> COMPLETE`
- Failure state: `FAILED`

## Step 3: Check Job Status
- UI: Jobs list.
- API: `GET /api/v1/jobs`

Track:
- `status`
- `current_stage`
- linked report status if present.

## Step 4: Work Review Tasks
- UI: Review queue and review detail.
- API:
  - `GET /api/v1/review/tasks?status=pending`
  - `GET /api/v1/review/tasks/{task_id}`
  - `GET /api/v1/review/documents/{document_id}/pages/{page_number}/image`

Submit corrections:
- `POST /api/v1/review/tasks/{task_id}/corrections`

Each corrected extracted field is also captured as a training sample with:
- `document_id`
- `page_number`
- `ocr_text`
- `span_start` / `span_end` (when original extracted text is found)
- `corrected_value`
- `field_name`
- `source` (`field_correction` or `classification_override`)
- `taxonomy_code` (for classification corrections)
- `reviewer`
- `created_at`

Mark complete (no field edits):
- `PATCH /api/v1/review/tasks/{task_id}/complete`

Both actions trigger downstream rerun (classification/reporting).

## Step 5: Generate/Export And Download Report
- List reports: `GET /api/v1/reports`
- Regenerate/export CSV: `POST /api/v1/reports/{report_id}/export`
- Download CSV: `GET /api/v1/reports/{report_id}/download`
- Schema reference: `GET /api/v1/reports/schema`

For batch uploads:
- Combined export: `POST /api/v1/batches/{batch_id}/reports/export`
- The resulting CSV contains rows from all accepted documents in that batch.
- Missing required DEFRA values stay blank and are reported as warnings; export is not blocked.

## Confidence Thresholds
- OCR review task when OCR token/block confidence is `< 0.70`.
- Classification review task when classification confidence is `< 0.85`.
- Extraction review task when required fields are missing or ambiguous.

## Interpreting Common Validation Errors
- `400 Unsupported mime type`: file type is not accepted.
- `400 File exceeds max size`: file is over configured limit.
- `400 Uploaded size does not match expected size`: presign metadata and uploaded bytes differ.
- `401 Authentication required`: missing `X-User-Id` or `X-Tenant-Id`.
- `403 Admin access required` or invalid tenant context.
- `404 Not found`: document/report/task not in your tenant or does not exist.
- `409 Idempotency conflict`: same key reused with different payload, or prior failed/in-progress key reuse.
- `410 Upload session has expired`: presigned session timed out.

ZIP-specific:
- `400 Unsupported ZIP mime type`: upload the archive as `application/zip`.
- Rejected ZIP entries are listed in the finalise response, for example:
  - `Unsupported file type`
  - `Path traversal entry is not allowed`
  - `Hidden files are not supported`
  - `Nested ZIP files are not supported`
  - `ZIP exceeds max file count`

## Manual CSV Submission (Current Pilot)
Phase 2 direct RPD/DEFRA API submission is not implemented.

Current approach:
1. Download CSV from `/api/v1/reports/{report_id}/download`.
2. Validate file internally against your reporting checklist.
3. Submit CSV manually through your existing DEFRA reporting process.
4. Keep `document_id`, `job_id`, and `report_id` for audit traceability.

## Export Corrections For Labelling

Export correction-derived training samples to JSONL:

```bash
python -m scripts.export_training_samples_jsonl \
  --output /tmp/packtrack_training_samples.jsonl
```

Optional filter:

```bash
python -m scripts.export_training_samples_jsonl \
  --reviewer qa-user \
  --output /tmp/packtrack_training_samples_qa.jsonl
```

Label Studio:
1. Create a new project.
2. Import the JSONL file.
3. Use `data.text` as source text and values under `meta` for routing/QA.

Example Label Studio config snippet:

```xml
<View>
  <Header value="Field: $meta.field_name" />
  <Header value="Corrected value: $meta.corrected_value" />
  <Text name="text" value="$text" />
  <Labels name="label" toName="text">
    <Label value="TargetEntity" />
  </Labels>
</View>
```

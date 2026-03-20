# PackTrack MVP (Local-First Pilot)

Local-first pilot stack for PackTrack:
- `frontend` (React + TypeScript)
- `api` (FastAPI)
- `worker` (pipeline orchestrator/worker API)
- `postgres`
- `redis`
- `minio`

Source-of-truth inputs:
- `docs/PackTrack_Plan.md`
- `data/defra/UK_DEFRA.xlsx` (taxonomy + report schema/column order)

## Pilot

Pilot operator documents:
- `docs/pilot/PILOT_ONBOARDING.md`
- `docs/pilot/DATA_REQUIREMENTS.md`
- `docs/pilot/PILOT_USER_GUIDE.md`
- `docs/pilot/SUPPORT_PROCESS.md`
- `docs/pilot/SECURITY_AND_PRIVACY.md`
- `docs/pilot/KNOWN_LIMITATIONS.md`
- `docs/pilot/PILOT_FEEDBACK_TEMPLATE.md`

## Prerequisites

- Docker + Docker Compose
- GNU Make

## Onboarding

1. Copy environment defaults:

```bash
cp .env.example .env
```

Confirm frontend API base includes versioned prefix:

```bash
VITE_API_BASE_URL=http://localhost:8000/api/v1
MINIO_INTERNAL_ENDPOINT=http://minio:9000
MINIO_PUBLIC_ENDPOINT=http://localhost:9000
```

2. Build and start services:

```bash
make build
make up
```

3. Run DB migrations and seed taxonomy:

```bash
make migrate
make seed
```

4. Verify health:

```bash
make health
```

## Auth and Idempotency

- Protected API endpoints require:
  - `X-User-Id`
  - `X-Tenant-Id`
- Hardened write endpoints support `Idempotency-Key`:
  - `POST /api/v1/documents/upload/finalise`
  - `POST /api/v1/pipeline/run/{document_id}`
  - `POST /api/v1/reports/{report_id}/export`
- Reusing the same key with a different payload is rejected.

## Pilot Runbook

### 1) Create upload URL

```bash
curl -sS -X POST "http://localhost:8000/api/v1/documents/upload/presign" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: pilot-user" \
  -H "X-Tenant-Id: 123456" \
  -d '{"filename":"invoice.pdf","mime_type":"application/pdf","size_bytes":12345}'
```

### 2) Upload file to the returned presigned URL

Use `curl --upload-file` with the `upload_url` from step 1.

### 3) Finalise upload (creates document + job + queue message)

```bash
curl -sS -X POST "http://localhost:8000/api/v1/documents/upload/finalise" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: pilot-user" \
  -H "X-Tenant-Id: 123456" \
  -H "Idempotency-Key: finalise-001" \
  -d '{"upload_id":"<upload_id>"}'
```

### 4) Run pipeline

```bash
curl -sS -X POST "http://localhost:8000/api/v1/pipeline/run/<document_id>" \
  -H "X-User-Id: pilot-user" \
  -H "X-Tenant-Id: 123456" \
  -H "Idempotency-Key: pipeline-001"
```

Pipeline stages:
- `QUEUED -> PREPROCESSING -> EXTRACTING -> CLASSIFYING -> REPORTING -> COMPLETE/FAILED`
- Stage retries are bounded by `PIPELINE_STAGE_MAX_ATTEMPTS` (default `3`).

### 5) Review tasks

- List queue:
  - `GET /api/v1/review/tasks?status=pending`
- Open detail:
  - `GET /api/v1/review/tasks/{task_id}`
- Submit corrections:
  - `POST /api/v1/review/tasks/{task_id}/corrections`
- Complete task:
  - `PATCH /api/v1/review/tasks/{task_id}/complete`

Corrections emit audit events and trigger downstream re-run (classify/report).

### 6) Reports

- List reports:
  - `GET /api/v1/reports`
- Export CSV:
  - `POST /api/v1/reports/{report_id}/export` (`Idempotency-Key` supported)
- Download CSV:
  - `GET /api/v1/reports/{report_id}/download`

Report download/export are tenant-isolated; cross-tenant access is rejected.

### 7) Batch Upload + Combined Report

The Upload screen accepts multiple files and can create one combined DEFRA CSV across the batch.

API flow:

1. Create a batch and presign each file:
   - `POST /api/v1/batches`
2. Upload each file to its returned `upload_url`
3. Finalise documents for the batch:
   - `POST /api/v1/batches/{batch_id}/finalise`
4. Run the pipeline for every document in the batch:
   - `POST /api/v1/batches/{batch_id}/run`
5. Export one combined DEFRA CSV:
   - `POST /api/v1/batches/{batch_id}/reports/export`

Combined reports:
- include all per-document DEFRA rows in one CSV
- keep the DEFRA header exactly as defined by `UK_DEFRA.xlsx`
- do not block on missing required fields; blanks are left in place and warnings are attached to the report

### 8) ZIP Batch Upload + Combined Report

The Upload screen also supports `ZIP batch` mode. This uses the same batch tables, pipeline run endpoint, and combined report exporter as the multi-file flow.

ZIP API flow:

1. Presign one ZIP upload:
   - `POST /api/v1/batches/upload-zip/presign`
2. Upload the ZIP to its returned `upload_url`
3. Finalise and extract supported files:
   - `POST /api/v1/batches/{batch_id}/finalise-zip`
4. Run the pipeline for accepted documents:
   - `POST /api/v1/batches/{batch_id}/run`
5. Export one combined DEFRA CSV:
   - `POST /api/v1/batches/{batch_id}/reports/export`

ZIP constraints:
- accepted inner file types: `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tiff`
- rejected: directories, hidden files, executables, nested ZIPs, path traversal entries
- limits:
  - `ZIP_MAX_FILE_COUNT`
  - `ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES`
  - `MAX_UPLOAD_SIZE_BYTES` per extracted file

Example local API flow:

```bash
ZIP_PATH=/tmp/packtrack-fixtures.zip
zip -j "$ZIP_PATH" \
  tests/fixtures/invoices/invoice_table_top_left.pdf \
  tests/fixtures/invoices/invoice_right_header_glass.pdf

PRESIGN_JSON=$(curl -sS -X POST "http://localhost:8000/api/v1/batches/upload-zip/presign" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: pilot-user" \
  -H "X-Tenant-Id: 123456" \
  -H "Idempotency-Key: zip-presign-001" \
  -d "{\"filename\":\"$(basename "$ZIP_PATH")\",\"mime_type\":\"application/zip\",\"size_bytes\":$(stat -f%z "$ZIP_PATH"),\"name\":\"ZIP demo\"}")

BATCH_ID=$(printf '%s' "$PRESIGN_JSON" | jq -r '.batch_id')
UPLOAD_ID=$(printf '%s' "$PRESIGN_JSON" | jq -r '.upload_id')
UPLOAD_URL=$(printf '%s' "$PRESIGN_JSON" | jq -r '.upload_url')

curl -sS -X PUT "$UPLOAD_URL" \
  -H "Content-Type: application/zip" \
  --upload-file "$ZIP_PATH"

curl -sS -X POST "http://localhost:8000/api/v1/batches/$BATCH_ID/finalise-zip" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: pilot-user" \
  -H "X-Tenant-Id: 123456" \
  -H "Idempotency-Key: zip-finalise-001" \
  -d "{\"upload_id\":\"$UPLOAD_ID\"}"

curl -sS -X POST "http://localhost:8000/api/v1/batches/$BATCH_ID/run" \
  -H "X-User-Id: pilot-user" \
  -H "X-Tenant-Id: 123456" \
  -H "Idempotency-Key: zip-run-001"

curl -sS -X POST "http://localhost:8000/api/v1/batches/$BATCH_ID/reports/export" \
  -H "X-User-Id: pilot-user" \
  -H "X-Tenant-Id: 123456" \
  -H "Idempotency-Key: zip-export-001"
```

## Backup and Restore

Scripts:
- `scripts/backup_postgres.sh`
- `scripts/restore_postgres.sh`
- `scripts/backup_minio.sh`
- `scripts/restore_minio.sh`

Make targets:
- `make backup-postgres`
- `make restore-postgres FILE=backups/<timestamp>/postgres.sql`
- `make backup-minio`
- `make restore-minio FILE=backups/<timestamp>/minio-data.tar.gz`

Examples:

```bash
make backup-postgres
make backup-minio
make restore-postgres FILE=backups/20260304T150000Z/postgres.sql
make restore-minio FILE=backups/20260304T150000Z/minio-data.tar.gz
```

## Developer Commands

- Format Python:
  - `make format`
  - `make format-changed`
  - `make format-file FILE=api/app/routers/documents.py`
- Frontend build in container:
  - `make frontend-build`
- Tests:
  - `make test` (fast default, excludes `slow` tests)
  - `make test-slow` (full suite including Docker/Tesseract/OCR integration tests)

Run `make test` for day-to-day development and CI feedback. Run `make test-slow` before release-candidate checks or when changing OCR/fixture evaluation behaviour.

## Pilot Admin Metrics and Weekly Snapshot

- Admin-only pilot summary endpoint:
  - `GET /api/v1/admin/metrics/pilot-summary`
  - Required headers: `X-User-Id`, `X-Tenant-Id`, `X-User-Role: admin`
- Tenant NER toggle endpoint:
  - `PATCH /api/v1/admin/tenants/{tenant_id}/settings`
  - Body: `{ "ner_enabled": true|false }`

Weekly snapshot command (writes JSON + CSV under `/tmp/packtrack_pilot_snapshots/`):

```bash
docker compose --env-file .env run --rm api python -m scripts.pilot_weekly_snapshot
```

## Training Data Export

Review corrections are captured into `training_samples` and can be exported for labelling.

Run from the API container or API working directory:

```bash
python -m scripts.export_training_samples_jsonl \
  --output /tmp/packtrack_training_samples.jsonl
```

Optional reviewer filter:

```bash
python -m scripts.export_training_samples_jsonl \
  --reviewer qa-user \
  --output /tmp/packtrack_training_samples_qa.jsonl
```

Import the JSONL into Label Studio as tasks (each line has `data.text` plus field metadata).

## Label Studio (Local only)

`labelstudio` is behind the `local` profile so default startup is unchanged.

Default stack (no Label Studio):

```bash
docker compose --env-file .env up -d
```

Start Label Studio locally when needed:

```bash
docker compose --env-file .env --profile local up -d labelstudio
```

Open:

```text
http://localhost:8080
```

Stop/remove only the Label Studio local service:

```bash
docker compose --env-file .env --profile local rm -fsv labelstudio
```

## Notes

- OCR/NER/classifier training is intentionally out of scope for this pilot branch.
- Report CSV column names and order are locked to `UK_DEFRA.xlsx`.

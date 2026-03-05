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

## Prerequisites

- Docker + Docker Compose
- GNU Make

## Onboarding

1. Copy environment defaults:

```bash
cp .env.example .env
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
  - `make test`

## Notes

- OCR/NER/classifier training is intentionally out of scope for this pilot branch.
- Report CSV column names and order are locked to `UK_DEFRA.xlsx`.

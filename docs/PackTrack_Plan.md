# PackTrack MVP — Local-First Pilot Build Plan

## Context

PackTrack automates UK DEFRA Extended Producer Responsibility (EPR) compliance for SMEs. The EPR deadline is April 2026 and 50k+ SMEs are newly in-scope. This plan scaffolds a local-first pilot (Docker Compose on a single VPS) matching the business plan's five-stage pipeline and intended AWS production architecture.

The project is **greenfield** — no existing code. The DEFRA Excel file (`/Users/Yuwa/Downloads/UK DEFRA.xlsx`) is the authoritative source for the 15-column CSV schema and 47-entry taxonomy (7 categories).

---

## Deliverable 1: High-Level Architecture + Data Flow

```
                        +------------------+
                        |  Frontend (SPA)  |
                        |  React + Vite    |
                        |  :5173           |
                        +--------+---------+
                                 |
                        REST / JSON (v1)
                                 |
                        +--------v---------+
                        |  API Gateway     |
                        |  FastAPI  :8000  |
                        +--------+---------+
                                 |
                 +---------------+----------------+
                 |                                |
        +--------v---------+           +----------v----------+
        |  Redis :6379     |           |  Postgres 15 :5432  |
        |  (message queue) |           |  (all tables)       |
        +--------+---------+           +---------------------+
                 |
        +--------v---------+
        |   Orchestrator   |
        |  (state machine) |
        +--------+---------+
                 |
   +------+------+------+------+
   |      |      |      |      |
+--v--+ +-v--+ +-v--+ +-v--+ +-v--+
|Ingest| |Pre | |Ext | |Cls | |Rpt |
|      | |proc| |ract| |sify| |    |
+--+---+ +--+-+ +--+-+ +--+-+ +--+-+
   |        |      |      |      |
   +--------+------+------+------+
                 |
        +--------v---------+
        |  MinIO (S3)      |
        |  :9000 / :9001   |
        |  Buckets:        |
        |   raw-uploads    |
        |   preprocessed   |
        |   ocr-output     |
        |   reports        |
        +------------------+

        +------------------+     +------------------+
        | Prometheus :9090 | --> | Grafana :3000    |
        +------------------+     +------------------+
```

### Per-Stage Data Flow

**Stage 1 — Ingest:** User uploads PDF/JPG/PNG/TIFF via `/api/v1/documents/upload`. Validate MIME + size (<50MB). Store original → MinIO `raw-uploads/{org_id}/{job_id}/`. If PDF: split to per-page PNGs via pdf2image (poppler). Create `documents` + `jobs` rows. Emit `DOCUMENT_INGESTED` audit event. Enqueue → `preprocess`.

**Stage 2 — Preprocess:** Load page images via OpenCV. Greyscale → adaptive threshold (Otsu/Gaussian) → Hough deskew (±15°) → CLAHE → median blur (kernel=3). Store → MinIO `preprocessed/`. Record quality metrics. Emit `DOCUMENT_PREPROCESSED`. Enqueue → `extract`.

**Stage 3 — Extract:** Tesseract 5.x OCR (PSM-6) per page. Capture per-token confidence. Flag tokens <0.70 for review. spaCy transformer NER (entity types: ORG_ID, MATERIAL, WEIGHT, UNIT, ACTIVITY_CODE, PACKAGING_TYPE, PACKAGING_CLASS, NATION, PERIOD). Regex normalisation (weight units, code casing, period formats). Store structured JSON → MinIO `ocr-output/`. Create `review_items` (type=OCR_REVIEW) if low-confidence. Emit `DOCUMENT_EXTRACTED`. Enqueue → `classify` or pause for review.

**Stage 4 — Classify:** Rules engine matches taxonomy codes. If confidence ≥0.85: auto-classify. Else: XGBoost fallback. If still <0.85: create `review_items` (type=CLASSIFICATION_REVIEW) with top-3 suggestions. Map to 15-column DEFRA schema. Store → `packaging_data` table. Emit `DOCUMENT_CLASSIFIED`. Enqueue → `report` or pause for review.

**Stage 5 — Report & Audit:** Pre-export validator checks: required columns, types, taxonomy code existence, cross-field rules (Plastic → subtype required; SP → org_size=S). Critical errors block export. Generate CSV with exact 15 columns in order. Store → MinIO `reports/`. Emit `REPORT_GENERATED`.

**Human Review Sub-Flow:** Orchestrator detects `review_items` → job status = `AWAITING_REVIEW`. Frontend shows review queue. Reviewer corrects via `/api/v1/review/{item_id}/resolve`. Original preserved in `review_item.original_value`. Emit `REVIEW_ITEM_RESOLVED`. Re-enqueue paused stage.

---

## Deliverable 2: Service Boundaries + API Contracts

### Docker Services (13 total)

| Service | Container | Responsibility | Port |
|---|---|---|---|
| `api` | `packtrack-api` | HTTP gateway, auth, validation | 8000 |
| `orchestrator` | `packtrack-orchestrator` | Pipeline state machine, dispatch | internal |
| `worker-ingest` | `packtrack-worker-ingest` | File ingestion, PDF splitting | internal |
| `worker-preprocess` | `packtrack-worker-preprocess` | OpenCV image pipeline | internal |
| `worker-extract` | `packtrack-worker-extract` | OCR + NER | internal |
| `worker-classify` | `packtrack-worker-classify` | Rules + XGBoost | internal |
| `worker-report` | `packtrack-worker-report` | CSV generation, validation | internal |
| `postgres` | `packtrack-postgres` | Primary data store | 5432 |
| `redis` | `packtrack-redis` | Message queue | 6379 |
| `minio` | `packtrack-minio` | Object storage | 9000/9001 |
| `frontend` | `packtrack-frontend` | React SPA | 5173 |
| `prometheus` | `packtrack-prometheus` | Metrics | 9090 |
| `grafana` | `packtrack-grafana` | Dashboards | 3000 |

### Inter-Stage Message Envelope (versioned JSON)

```json
{
  "schema_version": "1.0.0",
  "message_id": "uuid-v4",
  "timestamp": "ISO-8601",
  "source_stage": "ingest",
  "target_stage": "preprocess",
  "job_id": "uuid-v4",
  "document_id": "uuid-v4",
  "organisation_id": 123456,
  "idempotency_key": "uuid-v4",
  "attempt": 1,
  "max_attempts": 3,
  "payload": { }
}
```

### Queue Names

| Queue | Route |
|---|---|
| `packtrack:queue:ingest` | Upload → Ingest |
| `packtrack:queue:preprocess` | Ingest → Preprocess |
| `packtrack:queue:extract` | Preprocess → Extract |
| `packtrack:queue:classify` | Extract → Classify |
| `packtrack:queue:report` | Classify → Report |
| `packtrack:queue:review` | Any → Human review notification |
| `packtrack:queue:dead-letter` | Failed after max retries |

---

## Deliverable 3: Monorepo Structure

```
/PackTrack MVP/
├── frontend/                          # React SPA (Vite + TypeScript)
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── src/
│       ├── main.tsx, App.tsx
│       ├── api/                       # client.ts, types.ts
│       ├── components/
│       │   ├── layout/                # AppShell, Sidebar, Header
│       │   ├── upload/                # UploadDropzone, UploadProgress
│       │   ├── jobs/                  # JobsTable, JobStatusBadge, JobDetail
│       │   ├── review/               # DocumentViewer, OcrTokenEditor, ClassificationChooser, ReviewQueue
│       │   ├── reports/              # ExportPanel, ValidationSummary
│       │   └── audit/                # AuditLogViewer, AuditEventRow
│       ├── pages/                     # UploadPage, JobsPage, ReviewPage, ReportsPage, AuditPage
│       ├── hooks/                     # useJobs, useReviewItems, useAuditLog
│       └── utils/                     # formatting, constants
│
├── api/                               # FastAPI API gateway
│   ├── pyproject.toml
│   ├── Dockerfile
│   └── app/
│       ├── main.py                    # App factory
│       ├── config.py                  # Pydantic Settings
│       ├── dependencies.py
│       ├── routers/                   # documents, jobs, review, reports, audit, taxonomy, health, rpd_stub
│       ├── schemas/                   # Pydantic request/response models per router
│       ├── middleware/                # error_handler, request_id, metrics
│       └── services/                  # storage.py (MinIO), queue.py (Redis)
│
├── workers/                           # Pipeline stage workers
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── common/                        # base_worker.py, queue_consumer.py, metrics.py
│   ├── ingest/                        # worker.py, pdf_splitter.py, mime_validator.py
│   ├── preprocess/                    # worker.py, pipeline.py, deskew.py, enhance.py
│   ├── extract/                       # worker.py, ocr_engine.py, ner_pipeline.py, normaliser.py, confidence_scorer.py
│   ├── classify/                      # worker.py, rules_engine.py, ml_classifier.py, taxonomy_matcher.py
│   └── report/                        # worker.py, csv_generator.py, validator.py, schema_definitions.py
│
├── orchestrator/                      # Pipeline state machine
│   ├── pyproject.toml
│   ├── Dockerfile
│   └── app/
│       ├── state_machine.py
│       ├── dispatcher.py
│       ├── retry_policy.py
│       └── idempotency.py
│
├── shared/                            # Shared library (editable package)
│   ├── pyproject.toml
│   └── packtrack_shared/
│       ├── db/                        # models.py, session.py, migrations/ (Alembic)
│       ├── schemas/                   # message_envelope.py, defra_columns.py
│       ├── storage/                   # minio_client.py (S3 abstraction)
│       ├── queue/                     # redis_queue.py (SQS abstraction)
│       ├── audit/                     # event_store.py, event_types.py
│       ├── taxonomy/                  # loader.py, validator.py
│       ├── rpd/                       # client.py (Phase 2 stub interface)
│       ├── constants.py
│       └── exceptions.py
│
├── infra/
│   ├── docker-compose.yml
│   ├── docker-compose.override.yml
│   ├── .env.example
│   ├── Makefile
│   ├── docker/                        # Dockerfile.api, .worker, .orchestrator, .frontend
│   ├── postgres/                      # init.sql
│   ├── prometheus/                    # prometheus.yml
│   ├── grafana/provisioning/          # dashboards + datasources
│   └── minio/                         # create-buckets.sh
│
├── data/
│   ├── defra/                         # UK_DEFRA.xlsx (copied from downloads)
│   ├── seed/                          # seed_taxonomy.py, seed_test_data.py
│   └── sample_docs/                   # Sample PDFs/images for testing
│
├── ml/
│   ├── ner/                           # label_studio_config.xml, train_ner.py, evaluate_ner.py, export_model.py
│   └── classifier/                    # train_xgboost.py, evaluate_classifier.py, feature_engineering.py
│
├── tests/
│   ├── conftest.py
│   ├── unit/workers/                  # test_ingest, test_preprocess, test_extract, test_classify, test_report
│   ├── unit/shared/                   # test_taxonomy_validator, test_csv_schema, test_audit_events
│   ├── integration/                   # test_pipeline_e2e, test_api_contracts, test_queue_messages
│   └── contract/                      # test_message_schemas, test_api_schemas
│
├── docs/                              # ARCHITECTURE, API_REFERENCE, DEPLOYMENT, PILOT_RUNBOOK, DATA_MODEL, REVIEW_WORKFLOW
├── .gitignore
├── README.md
└── pyproject.toml                     # Root workspace config
```

---

## Deliverable 4: Data Model

### Tables + Indexes

**`organisations`** — `id SERIAL PK`, `organisation_id INT UNIQUE`, `name TEXT`, `organisation_size CHAR(1) CHECK (L/S)`, `created_at`, `updated_at`. Index: `organisation_id`.

**`taxonomy_versions`** — `id SERIAL PK`, `version_label TEXT UNIQUE` (e.g. "2025-03-01-initial"), `source_file TEXT`, `source_checksum TEXT` (SHA-256), `imported_at TIMESTAMPTZ`, `is_active BOOLEAN`, `notes TEXT`.

**`taxonomy_entries`** — `id SERIAL PK`, `taxonomy_version_id FK→taxonomy_versions`, `category TEXT`, `code TEXT`, `description TEXT`. Unique: `(taxonomy_version_id, category, code)`. Indexes: `taxonomy_version_id`, `(category, code)`.

**`documents`** — `id UUID PK`, `organisation_id FK`, `original_filename TEXT`, `mime_type TEXT`, `file_size_bytes BIGINT`, `storage_path TEXT`, `page_count INT`, `uploaded_at`, `uploaded_by TEXT`.

**`jobs`** — `id UUID PK`, `document_id FK→documents`, `organisation_id INT`, `status job_status ENUM` (CREATED, INGESTED, PREPROCESSING, PREPROCESSED, EXTRACTING, EXTRACTED, CLASSIFYING, CLASSIFIED, AWAITING_REVIEW, GENERATING_REPORT, REPORT_GENERATED, FAILED, CANCELLED), `current_stage TEXT`, `attempt_count INT`, `idempotency_key UUID UNIQUE`, `error_message TEXT`, `started_at`, `completed_at`, `created_at`, `updated_at`. Indexes: `status`, `organisation_id`, `document_id`, `idempotency_key`.

**`job_steps`** — `id UUID PK`, `job_id FK→jobs`, `stage TEXT`, `status TEXT CHECK (PENDING/RUNNING/COMPLETED/FAILED/SKIPPED)`, `attempt INT`, `idempotency_key UUID`, `input_payload JSONB`, `output_payload JSONB`, `metrics JSONB`, `error_message TEXT`, timestamps. Unique: `(idempotency_key, stage, attempt)`.

**`packaging_data`** — `id SERIAL PK`, `job_id FK→jobs`, `row_index INT`, `taxonomy_version_id FK→taxonomy_versions`, then **exactly the 15 DEFRA columns**: `organisation_id INT`, `subsidiary_id TEXT`, `organisation_size CHAR(1)`, `submission_period TEXT`, `packaging_activity TEXT`, `packaging_type TEXT`, `packaging_class TEXT`, `packaging_material TEXT`, `packaging_material_subtype TEXT`, `from_country TEXT`, `to_country TEXT`, `packaging_material_weight NUMERIC(12,2)`, `packaging_material_units INT`, `transitional_packaging_units INT`, `ram_rag_rating TEXT`. Plus: `classification_confidence REAL`, `classification_method TEXT CHECK (rules/xgboost/human)`, `is_validated BOOLEAN`.

**`review_items`** — `id UUID PK`, `job_id FK→jobs`, `review_type ENUM (OCR_REVIEW/CLASSIFICATION_REVIEW/VALIDATION_REVIEW)`, `status ENUM (PENDING/IN_PROGRESS/RESOLVED/SKIPPED)`, `stage TEXT`, `page_number INT`, `field_name TEXT`, `original_value TEXT`, `suggested_values JSONB`, `corrected_value TEXT`, `confidence REAL`, `reviewer TEXT`, `context JSONB`, `resolved_at`, `created_at`.

**`audit_events`** (APPEND-ONLY) — `id BIGSERIAL PK`, `event_id UUID UNIQUE`, `event_type TEXT`, `entity_type TEXT`, `entity_id TEXT`, `job_id UUID`, `organisation_id INT`, `actor TEXT DEFAULT 'system'`, `payload JSONB`, `created_at TIMESTAMPTZ`. **Database triggers prevent UPDATE and DELETE.** Indexes: `(entity_type, entity_id)`, `job_id`, `event_type`, `created_at`, `organisation_id`.

**`reports`** — `id UUID PK`, `job_id FK→jobs`, `organisation_id INT`, `submission_period TEXT`, `taxonomy_version_id FK`, `csv_storage_path TEXT`, `row_count INT`, `validation_summary JSONB`, `is_valid BOOLEAN`, `generated_at`.

---

## Deliverable 5: FastAPI Endpoints

All prefixed `/api/v1`. Standard envelope: `{ success, data, error, meta }`.

### Documents
- `POST /documents/upload` — multipart, accepts PDF/JPG/PNG/TIFF, max 50MB → 201 `{ document_id, job_id, status }`
- `GET /documents` — paginated list, filter by org
- `GET /documents/{id}` — detail with job ref
- `GET /documents/{id}/pages/{n}/image?stage=raw|preprocessed` — presigned MinIO URL

### Jobs
- `GET /jobs` — paginated, filter by status/org
- `GET /jobs/{id}` — detail with steps + review count
- `GET /jobs/{id}/steps` — step timeline with metrics
- `POST /jobs/{id}/rerun` — body `{ from_stage }`, new idempotency key → 202
- `POST /jobs/{id}/cancel` → 200

### Review
- `GET /review/items` — paginated, filter by status/type/job
- `GET /review/items/{id}` — detail with suggestions + context
- `PATCH /review/items/{id}/resolve` — body `{ corrected_value, reviewer }` → re-enqueue pipeline
- `PATCH /review/items/{id}/skip` — body `{ reason, reviewer }`
- `GET /review/stats` — pending/in_progress/resolved/avg_resolution_time

### Reports
- `POST /reports/generate` — body `{ job_id, organisation_id, submission_period }` → 202
- `GET /reports/{id}` — detail with validation summary
- `GET /reports/{id}/download` — presigned CSV URL
- `GET /reports/{id}/validation` — detailed validation report
- `POST /reports/validate-preview` — body `{ job_id }`, dry run validation

### Audit
- `GET /audit/events` — paginated, filter by entity/type/job/org/date
- `GET /audit/events/{id}` — full payload
- `GET /audit/jobs/{id}/timeline` — ordered events for entire job

### Taxonomy
- `GET /taxonomy/versions` — list versions
- `GET /taxonomy/versions/{id}/entries?category=` — entries
- `GET /taxonomy/lookup?category=&code=` — single lookup
- `POST /taxonomy/validate-codes` — bulk validation

### Health
- `GET /health` → `{ status, version }`
- `GET /health/ready` → checks Postgres, Redis, MinIO

### RPD Stub (Phase 2)
- `POST /rpd/submit` → 501 Not Implemented
- `GET /rpd/submission-status/{id}` → 501 Not Implemented

---

## Deliverable 6: Human Review UI Screens

| # | Route | Components | Key Behaviour |
|---|---|---|---|
| 1 | `/upload` | UploadDropzone, UploadProgress | Drag-drop PDF/JPG/PNG/TIFF. Org selector. Progress bar → redirect to job. |
| 2 | `/jobs` | JobsTable, JobStatusBadge | Filterable table: ID, doc name, org, status badge, stage, created, review count. |
| 3 | `/jobs/:id` | JobDetail, JobStepTimeline | Vertical stepper per stage. Metrics expand. AWAITING_REVIEW banner. Re-run/Cancel/Download. |
| 4 | `/review` | ReviewQueue | Table of pending items sorted by confidence (worst-first). Filter by type/status. Stats bar. |
| 5 | `/review/:id` (OCR) | DocumentViewer, OcrTokenEditor | Split-pane: zoomable image left, annotated OCR text right. Low-conf tokens amber. Edit + accept per token. Keyboard: Tab/Enter. |
| 6 | `/review/:id` (classify) | ClassificationChooser | 15-column form. Flagged fields show top-3 dropdown with confidence %. Accept/override. Bulk-accept button. |
| 7 | `/audit` | AuditLogViewer, AuditEventRow | Chronological infinite scroll. Colour-coded type chips. Collapsible JSON payload. Filters: type, entity, org, date. |
| 8 | `/reports` | ExportPanel, ValidationSummary | Report list with validation status. Generate modal → validation preview → Generate & Download. Filename: `{org_id}_{period}_packaging_data.csv`. |

---

## Deliverable 7: Orchestration — Retries, Idempotency, Re-runs

### State Machine
```
CREATED → INGESTED → PREPROCESSING → PREPROCESSED → EXTRACTING → EXTRACTED
  → CLASSIFYING → CLASSIFIED → GENERATING_REPORT → REPORT_GENERATED

Any stage with review items: → AWAITING_REVIEW → (resume) → next stage
Any stage on failure: → FAILED (after 3 retries)
User-initiated: → CANCELLED
```

### Retry Policy
- `max_attempts = 3`, exponential backoff (5s base, 2x multiplier, 300s max, + jitter)
- Retryable: OCRTimeout, MinIOConnection, DatabaseConnection
- Non-retryable: InvalidDocument, UnsupportedFormat, TaxonomyValidation

### Idempotency
- Every job step keyed by `(idempotency_key, stage, attempt)`.
- `BaseWorker.process_message()` checks existing step before executing. If COMPLETED → skip (no-op).
- All workers inherit from `BaseWorker` ABC → `execute()` method.

### Re-runs Without Breaking Immutability
1. New `idempotency_key` generated for re-run
2. Original `job_steps` records **never modified** — preserved as history
3. New `job_steps` with `attempt = prev_max + 1`
4. `PIPELINE_RERUN_REQUESTED` audit event emitted
5. Report generator uses only latest attempt's data
6. Previous `packaging_data` rows kept; new rows created

---

## Deliverable 8: docker-compose.yml + .env.example + Makefile

**docker-compose.yml** — 13 services as in Deliverable 2. Key details:
- All workers share `Dockerfile.worker`, differentiated by `command` and env vars (`WORKER_STAGE`, `WORKER_QUEUE`)
- `minio-init` sidecar creates buckets on startup
- Health checks on Postgres, Redis, MinIO with `condition: service_healthy`
- Named volumes: `pgdata`, `miniodata`, `grafanadata`, `spacy_models`, `xgboost_models`
- Single `packtrack` bridge network

**.env.example** — Postgres creds, Redis port, MinIO creds, API port, frontend port, confidence thresholds (OCR=0.70, classification=0.85), Tesseract PSM=6, Prometheus/Grafana ports, max upload size.

**Makefile targets**: `up`, `down`, `build`, `rebuild`, `logs`, `logs-api`, `logs-workers`, `seed`, `migrate`, `migrate-create`, `test`, `test-unit`, `test-integration`, `test-contract`, `lint`, `format`, `clean`, `psql`, `redis-cli`, `health`.

---

## Deliverable 9: Testing Plan

### Contract Tests (`/tests/contract/`)
- `test_message_schemas.py` — round-trip MessageEnvelope + per-stage payloads; strict mode rejects unknown fields; schema version mismatch raises error
- `test_api_schemas.py` — validate API response shapes match OpenAPI spec

### Unit Tests (`/tests/unit/`)
- `test_ingest` — PDF page count, MIME validation, MinIO path convention
- `test_preprocess` — deskew on rotated test images, CLAHE output, greyscale/colour handling
- `test_extract` — confidence scorer flags <0.70, regex normaliser (weights, codes, periods), NER entity types
- `test_classify` — rules match exact codes, flags invalid codes, XGBoost stub shape, confidence threshold routing
- `test_report` — CSV has exactly 15 columns in order, correct headers, validator catches: missing required, invalid codes, Plastic without subtype, non-numeric weight
- `test_taxonomy_validator` — all 47 entries load, lookup per category, invalid → None, version filtering
- `test_csv_schema` — hardcoded assertion on exact column name list
- `test_audit_events` — UPDATE/DELETE raises, event creation stores payload

### Integration Tests (`/tests/integration/`)
- `test_pipeline_e2e` — upload sample PDF → REPORT_GENERATED, CSV in MinIO, correct columns, full audit trail
- `test_api_contracts` — every endpoint with valid/invalid inputs, status codes, error shapes
- `test_queue_messages` — publish → correct worker picks up; idempotency → single execution

### Pilot KPI Metrics (Prometheus → Grafana)
| KPI | Metric | Target |
|---|---|---|
| OCR accuracy | `packtrack_ocr_confidence_mean` | >0.90 |
| Low-conf token rate | `packtrack_ocr_low_confidence_ratio` | <0.10 |
| Auto-classify rate | `packtrack_classification_auto_rate` | >0.85 |
| Report gen time | `packtrack_report_generation_seconds` | <1800s |
| Stage failure rate | `packtrack_stage_failures_total` | <0.05 |
| Queue depth, active jobs, throughput, review resolution time | various | monitored |

---

## Deliverable 10: Phase 2 RPD Stub

- `POST /api/v1/rpd/submit` → HTTP 501 with message directing to manual CSV download
- `GET /api/v1/rpd/submission-status/{id}` → HTTP 501
- `/shared/packtrack_shared/rpd/client.py` defines `RPDClientInterface` ABC with methods: `authenticate()`, `submit_report()`, `get_submission_status()`. `RPDClientStub` raises `NotImplementedError`.
- Commented-out `rpd_submissions` table schema ready for Phase 2 migration.

---

## AWS Parity Mapping

| Local Pilot Component | AWS Production Service |
|---|---|
| MinIO (raw-uploads, preprocessed, ocr-output, reports buckets) | **S3** |
| FastAPI workers (worker-ingest, -preprocess, -extract, -classify, -report) | **Lambda** (light) / **ECS Fargate** (heavy, e.g. extract) |
| spaCy model file mount (Docker volume) | **SageMaker endpoint** |
| Postgres 15 | **RDS Postgres** |
| FastAPI API gateway (`packtrack-api`) | **API Gateway** + Lambda |
| Redis queues (`packtrack:queue:*`) | **SQS** |
| Orchestrator state machine | **Step Functions** |
| `.env` / Docker secrets | **Secrets Manager** |
| Prometheus + Grafana | **CloudWatch** (metrics + dashboards) |
| XGBoost model file mount | **SageMaker endpoint** |

---

## NER Training Loop Stubs (`/ml/ner/`)

1. `label_studio_config.xml` — Label Studio project config for BILOU entity annotation (entity types: ORG_ID, MATERIAL, WEIGHT, UNIT, ACTIVITY_CODE, PACKAGING_TYPE, PACKAGING_CLASS, NATION, PERIOD)
2. `train_ner.py` — Stub: load annotations from Label Studio export → convert to spaCy DocBin → fine-tune `en_core_web_trf` (RoBERTa) → save model → log metrics
3. `evaluate_ner.py` — Stub: load test set → run model → compute precision/recall/F1 per entity type → target >92% F1
4. `export_model.py` — Stub: version model → register in `/models/ner/{version}/` → update config to point to latest

XGBoost stubs in `/ml/classifier/`: `train_xgboost.py`, `evaluate_classifier.py`, `feature_engineering.py`.

---

## Implementation Sequence

| Week | Focus |
|---|---|
| 1 | Monorepo scaffold, Docker Compose, Postgres schema + Alembic migrations, MinIO init, taxonomy seed script, shared library (DB models, queue, storage, audit) |
| 2 | API gateway (all routers with stubs, health, upload), orchestrator state machine, BaseWorker with idempotency/retry, ingest worker |
| 3 | Preprocess worker (OpenCV), extract worker (Tesseract + spaCy stubs), classify worker (rules + XGBoost stub), report worker (CSV generator + validator) |
| 4 | Frontend: upload, jobs dashboard, job detail, review queue, document viewer, OCR editor, classification chooser |
| 5 | Audit viewer, export page, Prometheus + Grafana dashboards, contract + integration tests |
| 6 | E2E testing with sample docs, pilot runbook, deployment docs |

---

## Verification

1. `make build && make up` — all 13 containers start healthy
2. `make seed` — taxonomy_versions + 47 taxonomy_entries populated in Postgres
3. `curl localhost:8000/api/v1/health/ready` → all checks pass
4. Upload a test PDF via `/api/v1/documents/upload` → job created, pipeline progresses through stages
5. Verify audit_events table has entries for each stage transition
6. Verify `UPDATE audit_events SET ...` fails (trigger)
7. Generate report → CSV has exactly 15 columns in correct order
8. Validation with invalid taxonomy code → export blocked with error report
9. `make test` — all contract, unit, integration tests pass
10. Grafana dashboard at `:3000` shows pipeline metrics

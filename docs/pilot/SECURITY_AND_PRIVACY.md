# Security And Privacy

## Tenant Isolation
- Document, report, and review-task access is tenant-scoped.
- API checks tenant context via `X-Tenant-Id` for protected endpoints.
- Cross-tenant access is rejected.

## Storage Overview
- Relational data in Postgres (documents, jobs, pages, extracted entities, classifications, review tasks, audit events, reports).
- File artefacts in object storage (MinIO/S3-compatible): raw uploads, page images, OCR artefacts, generated CSV files.

## Access Control In MVP
- Protected endpoints require `X-User-Id` and `X-Tenant-Id`.
- Admin telemetry endpoint additionally requires `X-User-Role: admin`:
  - `GET /api/v1/metrics/summary`
- This is pilot-grade header auth and must not be treated as production IAM.

## Data Retention (Pilot)
- Recommended baseline for pilot operations:
  - Keep artefacts and records for the active 30-day pilot plus 30 days for review and incident closure.
- Backups should be access-controlled and encrypted at rest by the hosting environment.

## Deletion Request Process
- No self-service deletion API is currently implemented.
- For deletion requests:
  1. Raise request via support process with tenant ID and document/report identifiers.
  2. Pilot operator performs controlled deletion from Postgres and object storage.
  3. Operator confirms completion and records it in support/audit trail.

## Demo Endpoint Restriction
- Demo endpoints are local-only gated by environment configuration.
- Demo endpoints are not available to pilot tenants in normal pilot environments.

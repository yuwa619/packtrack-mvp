# PackTrack MVP RC1 Release Notes

- Release: `packtrack-rc1`
- Date: `2026-03-05`
- Commit: `0348053` (RC1 payload snapshot)

## Included Verification Fixes
- Alembic migration runtime import-path fix in `api/alembic/env.py`.
- Hardening test stabilization for pipeline/report idempotency tenant test timeout.
- Makefile lint/format fallback to test venv `ruff` runner for deterministic local verification.

## Alembic Head Revision
- `20260304_0006`

## Docker Image Tags
- `packtrackmvp-api:latest`
- `packtrackmvp-worker:latest`
- `packtrackmvp-frontend:latest`
- `postgres:15`
- `redis:7-alpine`
- `minio/minio:latest`

## Demo Endpoint Gating Rule
- Demo endpoints are available when `ENVIRONMENT=local`.
- If `ENVIRONMENT` is not `local`, demo endpoints require `ENABLE_DEMO_ENDPOINTS=true`; otherwise they return `404`.

## Known Limitations
- Authentication is header-based (`X-User-Id`, `X-Tenant-Id`) for pilot usage and is not integrated with an external IdP.
- Local service image tags are mutable (`:latest`) for app services.
- Pipeline stages rely on confidence thresholds and can require manual review tasks for low-confidence OCR/classification outcomes.

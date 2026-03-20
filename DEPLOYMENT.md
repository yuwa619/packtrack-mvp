# PackTrack MVP — Deployment Guide

## Architecture

```
  Browser
    │
    ▼
┌──────────────┐       HTTPS        ┌──────────────────┐
│  Netlify     │ ──────────────────▶ │  Render          │
│  (frontend)  │                     │  packtrack-api   │
│  Static SPA  │                     │  FastAPI :8000   │
└──────────────┘                     └────────┬─────────┘
                                              │
                              ┌───────────────┼───────────────┐
                              │               │               │
                        ┌─────▼─────┐  ┌──────▼──────┐ ┌─────▼─────┐
                        │ Postgres  │  │   Redis     │ │ S3 / R2   │
                        │ (Render)  │  │  (Render)   │ │ (AWS/CF)  │
                        └───────────┘  └──────┬──────┘ └───────────┘
                                              │
                                     ┌────────▼────────┐
                                     │ packtrack-worker │
                                     │ (Render bg svc)  │
                                     └─────────────────┘
```

- **Frontend**: Static Vite build on Netlify CDN
- **API**: Docker-based FastAPI on Render (web service)
- **Worker**: Docker-based background service on Render
- **Postgres**: Render managed database
- **Redis**: Render managed Redis
- **Object Storage**: Any S3-compatible service (AWS S3, Cloudflare R2, Tigris, Backblaze B2)

Local Docker Compose development is unchanged.

---

## Prerequisites

- GitHub repo connected to both Netlify and Render
- An S3-compatible storage account with three buckets created:
  - `packtrack-raw-uploads`
  - `packtrack-preprocessed`
  - `packtrack-reports`
- Render account (Starter plan or above for managed Postgres)
- Netlify account (free tier is sufficient)

---

## Step 1: Deploy Backend on Render

### Option A: Blueprint (recommended)

1. Go to [Render Dashboard](https://dashboard.render.com) → **Blueprints** → **New Blueprint Instance**
2. Connect your GitHub repo
3. Render reads `render.yaml` and creates:
   - `packtrack-api` web service
   - `packtrack-worker` background worker
   - `packtrack-db` Postgres database
4. Create Redis manually in Render dashboard (Dashboard → New → Redis) if not auto-created
5. After services are created, set these environment variables in the Render dashboard:

| Variable | Service | Value |
|---|---|---|
| `MINIO_INTERNAL_ENDPOINT` | api | `https://s3.us-east-1.amazonaws.com` (or your S3 endpoint) |
| `MINIO_PUBLIC_ENDPOINT` | api | Same as above (browser-accessible S3 endpoint) |
| `MINIO_ACCESS_KEY` | api | Your S3 access key |
| `MINIO_SECRET_KEY` | api | Your S3 secret key |
| `MINIO_REGION` | api | Your S3 region (e.g. `us-east-1`) |
| `CORS_ORIGINS` | api | `https://YOUR-SITE.netlify.app` |
| `REDIS_URL` | api, worker | Auto-set if using blueprint; otherwise copy from Redis dashboard |

### Option B: Manual setup

1. **Postgres**: Dashboard → New → PostgreSQL → name: `packtrack-db`, plan: Starter
2. **Redis**: Dashboard → New → Redis → name: `packtrack-redis`, plan: Starter
3. **API**: Dashboard → New → Web Service
   - Docker, root dir: `api`, Dockerfile path: `./Dockerfile`
   - Set all env vars from the table above plus `DATABASE_URL` (from Postgres dashboard)
4. **Worker**: Dashboard → New → Background Worker
   - Docker, root dir: `worker`, Dockerfile path: `./Dockerfile`
   - Set `ENVIRONMENT=production`, `REDIS_URL` from Redis dashboard

### Run database migrations

After the API service deploys, open a **Shell** tab on the `packtrack-api` service in Render:

```bash
cd /app && alembic upgrade head
```

Or use the Render dashboard → `packtrack-api` → Shell.

Subsequent deploys: Render does not auto-run migrations. Add a release command
or run manually after each schema change. To add an auto-migration, set this in
the Render dashboard for the API service under **Pre-Deploy Command**:

```
cd /app && alembic upgrade head
```

### Verify backend health

```bash
curl https://packtrack-api.onrender.com/api/v1/health
# Expected: {"status":"ok","service":"api"}

curl https://packtrack-api.onrender.com/api/v1/health/ready
# Expected: checks for Postgres, Redis, S3 connectivity
```

---

## Step 2: Deploy Frontend on Netlify

1. Go to [Netlify Dashboard](https://app.netlify.com) → **Add new site** → **Import from Git**
2. Connect your GitHub repo
3. Netlify auto-detects `netlify.toml` with these settings:
   - **Base directory**: `frontend`
   - **Build command**: `npm ci && npm run build`
   - **Publish directory**: `frontend/dist`
4. Set environment variable in Netlify dashboard → **Site settings** → **Environment variables**:

| Variable | Value |
|---|---|
| `VITE_API_BASE_URL` | `https://packtrack-api.onrender.com/api/v1` |

5. Trigger a deploy (or it auto-deploys on push)

### Update CORS on Render

After your Netlify site is live (e.g. `https://packtrack-mvp.netlify.app`), update the
`CORS_ORIGINS` env var on the Render API service:

```
CORS_ORIGINS=https://packtrack-mvp.netlify.app
```

If you need multiple origins (e.g. preview deploys), comma-separate them:

```
CORS_ORIGINS=https://packtrack-mvp.netlify.app,https://deploy-preview-42--packtrack-mvp.netlify.app
```

---

## Step 3: Verify End-to-End

1. Open your Netlify URL in a browser
2. The app should load and show the upload/jobs interface
3. Upload a test document — it should:
   - Hit the Render API for presigned S3 URLs
   - Upload directly to S3
   - Trigger pipeline processing
   - Generate a report

---

## Environment Variables Reference

### Render — API Service

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | Yes | `production` | Runtime environment |
| `DATABASE_URL` | Yes | (from Render DB) | PostgreSQL connection string |
| `REDIS_URL` | Yes | (from Render Redis) | Redis connection string |
| `CORS_ORIGINS` | Yes | — | Comma-separated allowed origins |
| `MINIO_INTERNAL_ENDPOINT` | Yes | — | S3 endpoint for server-side ops |
| `MINIO_PUBLIC_ENDPOINT` | Yes | — | S3 endpoint for presigned URLs |
| `MINIO_ACCESS_KEY` | Yes | — | S3 access key |
| `MINIO_SECRET_KEY` | Yes | — | S3 secret key |
| `MINIO_SECURE` | No | `true` | Use HTTPS for S3 |
| `MINIO_REGION` | No | `us-east-1` | S3 region |
| `MINIO_BUCKET_RAW` | No | `packtrack-raw-uploads` | Raw uploads bucket |
| `MINIO_BUCKET_PREPROCESSED` | No | `packtrack-preprocessed` | Preprocessed images bucket |
| `MINIO_BUCKET_REPORTS` | No | `packtrack-reports` | Generated reports bucket |
| `MINIO_ALLOW_LOCAL_FALLBACK` | No | `false` | Disable in production |
| `OCR_CONFIDENCE_THRESHOLD` | No | `0.70` | OCR confidence threshold |
| `CLASSIFICATION_CONFIDENCE_THRESHOLD` | No | `0.85` | Classification threshold |
| `MAX_UPLOAD_SIZE_BYTES` | No | `52428800` | Max upload size (50MB) |
| `NER_ENABLED` | No | `false` | Enable spaCy NER |

### Render — Worker Service

| Variable | Required | Default | Description |
|---|---|---|---|
| `ENVIRONMENT` | Yes | `production` | Runtime environment |
| `REDIS_URL` | Yes | (from Render Redis) | Redis connection string |

### Netlify — Frontend

| Variable | Required | Default | Description |
|---|---|---|---|
| `VITE_API_BASE_URL` | Yes | — | Full Render API URL (e.g. `https://packtrack-api.onrender.com/api/v1`) |

---

## Deployment Order

1. **Render Postgres** — create database, note connection URI
2. **Render Redis** — create instance, note connection URI
3. **S3 buckets** — create three buckets, note endpoint and credentials
4. **Render API** — deploy with all env vars, run `alembic upgrade head`
5. **Render Worker** — deploy with `REDIS_URL`
6. **Verify API** — `curl .../api/v1/health`
7. **Netlify Frontend** — deploy with `VITE_API_BASE_URL`
8. **Update CORS** — set `CORS_ORIGINS` on Render API to match Netlify domain
9. **Verify E2E** — upload a test document through the UI

---

## S3-Compatible Storage Setup

The codebase uses the `minio` Python client, which is compatible with any S3 API.
No code changes are needed — just set the endpoint and credentials.

### AWS S3

```env
MINIO_INTERNAL_ENDPOINT=https://s3.us-east-1.amazonaws.com
MINIO_PUBLIC_ENDPOINT=https://s3.us-east-1.amazonaws.com
MINIO_ACCESS_KEY=AKIA...
MINIO_SECRET_KEY=...
MINIO_SECURE=true
MINIO_REGION=us-east-1
```

### Cloudflare R2

```env
MINIO_INTERNAL_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com
MINIO_PUBLIC_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_SECURE=true
MINIO_REGION=auto
```

### Tigris (Render-native)

```env
MINIO_INTERNAL_ENDPOINT=https://fly.storage.tigris.dev
MINIO_PUBLIC_ENDPOINT=https://fly.storage.tigris.dev
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_SECURE=true
MINIO_REGION=auto
```

### Local development (unchanged)

Local Docker Compose still uses MinIO on `localhost:9000`. No changes needed.

---

## Running Migrations in Production

Alembic reads `DATABASE_URL` from the environment (see `api/alembic/env.py`).

**Manual** (Render Shell):
```bash
cd /app && alembic upgrade head
```

**Automatic** (recommended): Set the Render API service **Pre-Deploy Command** to:
```
cd /app && alembic upgrade head
```

This runs migrations before each new deploy becomes live.

---

## Troubleshooting

### CORS errors in browser console
- Verify `CORS_ORIGINS` on Render matches your exact Netlify domain (including `https://`)
- Check for trailing slashes — the origin should not end with `/`

### S3 presigned URL errors
- Verify `MINIO_PUBLIC_ENDPOINT` is reachable from the browser
- For AWS S3, ensure the bucket has appropriate CORS policy allowing PUT from your Netlify domain
- Check that `MINIO_SECURE=true` in production

### Database connection errors
- Render Postgres connection strings use `postgresql://` — the app expects `postgresql+psycopg://`
- If Render provides `postgresql://...`, the app will still work as psycopg accepts both formats

### Worker not processing jobs
- Verify both API and Worker share the same `REDIS_URL`
- Check Worker logs in the Render dashboard

### Render free tier cold starts
- Render Starter plan services spin down after 15 minutes of inactivity
- First request after idle may take 30-60 seconds
- Upgrade to a paid plan for always-on services

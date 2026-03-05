#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_DIR="${1:-$ROOT_DIR/backups/$TIMESTAMP}"
OUTPUT_FILE="$OUTPUT_DIR/postgres.sql"

mkdir -p "$OUTPUT_DIR"

set -a
source "$ENV_FILE"
set +a

docker compose --env-file "$ENV_FILE" exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > "$OUTPUT_FILE"

echo "Postgres backup completed: $OUTPUT_FILE"

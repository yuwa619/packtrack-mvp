#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_DIR="${1:-$ROOT_DIR/backups/$TIMESTAMP}"
OUTPUT_FILE="$OUTPUT_DIR/minio-data.tar.gz"

mkdir -p "$OUTPUT_DIR"

docker compose --env-file "$ENV_FILE" exec -T minio \
  sh -lc "tar -czf - -C /data ." > "$OUTPUT_FILE"

echo "MinIO backup completed: $OUTPUT_FILE"

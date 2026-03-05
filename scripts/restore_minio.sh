#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /absolute/or/relative/path/to/minio-data.tar.gz"
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
INPUT_FILE="$1"

if [[ ! -f "$INPUT_FILE" ]]; then
  echo "Backup file not found: $INPUT_FILE"
  exit 1
fi

cat "$INPUT_FILE" | docker compose --env-file "$ENV_FILE" exec -T minio \
  sh -lc "rm -rf /data/* && tar -xzf - -C /data"

echo "MinIO restore completed from: $INPUT_FILE"

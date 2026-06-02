#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f "config/global.linux-docker.template.json" ]; then
  echo "[ERROR] Missing config/global.linux-docker.template.json"
  exit 1
fi

if [ -f "config/global.json" ]; then
  cp config/global.json "config/global.json.bak.$(date +%Y%m%d_%H%M%S)"
fi

cp config/global.linux-docker.template.json config/global.json

echo "[INFO] Switched config/global.json to Linux Docker template"
echo "[INFO] Starting container..."

docker compose -f docker-compose.linux.yml up -d --build

echo "[DONE] Linux Docker crawler is running"
echo "[HINT] Follow logs: docker compose -f docker-compose.linux.yml logs -f"

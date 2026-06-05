#!/usr/bin/env bash
set -euo pipefail
docker build -t mmogo/crawler:v1.0 -f Dockerfile .
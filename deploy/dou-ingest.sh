#!/usr/bin/env bash
# Baixa tudo o que saiu no DOU hoje (ou na data passada: --date AAAA-MM-DD).
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
set -a; [ -f "$APP_DIR/.env" ] && . "$APP_DIR/.env"; set +a
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/src/run_harness_loop.py" dou-ingest "$@"

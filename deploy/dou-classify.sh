#!/usr/bin/env bash
# Classifica os atos do DOU de hoje com o harness ativo (custa chamadas de API).
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
set -a; [ -f "$APP_DIR/.env" ] && . "$APP_DIR/.env"; set +a
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/src/run_harness_loop.py" dou-classify "$@"

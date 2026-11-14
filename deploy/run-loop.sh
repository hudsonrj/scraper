#!/usr/bin/env bash
# Roda o loop no servidor. Uso: deploy/run-loop.sh [--rounds 4] [--candidates 8] [--max-calls 5000]
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
set -a; [ -f "$APP_DIR/.env" ] && . "$APP_DIR/.env"; set +a
exec "$APP_DIR/.venv/bin/python" "$APP_DIR/src/run_harness_loop.py" loop "$@"

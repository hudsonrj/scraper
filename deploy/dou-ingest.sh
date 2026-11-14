#!/usr/bin/env bash
# Baixa tudo o que saiu no DOU. Sem argumentos: hoje e o dia útil anterior.
# Com --date AAAA-MM-DD: só esse dia. A fonte vem de DOU_SOURCE no .env
# (github = espelho do GitHub Actions, para servidores que o in.gov.br bloqueia).
set -uo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
set -a; [ -f "$APP_DIR/.env" ] && . "$APP_DIR/.env"; set +a
PY="$APP_DIR/.venv/bin/python"; CLI="$APP_DIR/src/run_harness_loop.py"
if [ $# -gt 0 ]; then exec "$PY" "$CLI" dou-ingest "$@"; fi
days="$("$PY" -c '
from datetime import date, timedelta
d = date.today(); p = d - timedelta(days=1)
while p.weekday() >= 5: p -= timedelta(days=1)
print(d.isoformat(), p.isoformat())')"
rc=0
for d in $days; do
  "$PY" "$CLI" dou-ingest --date "$d"; r=$?
  [ "$r" = 1 ] && rc=1   # 2 = dia sem publicação, não é erro
done
exit $rc

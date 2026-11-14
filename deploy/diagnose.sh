#!/usr/bin/env bash
# Diagnóstico do newsloop. Rodar como root no servidor e colar a saída na conversa.
DOMAIN="${DOMAIN:-newsloop.ch8ai.com.br}"
APP_DIR="${APP_DIR:-/root/apps/harnessend}"
h(){ printf '\n== %s\n' "$*"; }
h "Código";        ls -d "$APP_DIR" 2>&1; git -C "$APP_DIR" log --oneline -1 2>&1
h "Serviço";       systemctl is-active newsloop-web 2>&1; grep -o 'bind [^ ]*' /etc/systemd/system/newsloop-web.service 2>&1
PORT="$(grep -oP '127\.0\.0\.1:\K[0-9]+' /etc/systemd/system/newsloop-web.service 2>/dev/null | head -1)"
h "App local";     curl -sS -m 5 "http://127.0.0.1:${PORT:-8090}/saude" 2>&1
h "Logs do app";   journalctl -u newsloop-web -n 15 --no-pager 2>&1 | tail -15
h "Caddy no host"; command -v caddy; systemctl is-active caddy 2>&1; ls /etc/caddy 2>&1
grep -n -A3 -E "$DOMAIN|ch8ai|^:80|^http://" /etc/caddy/Caddyfile 2>/dev/null | head -40
h "Docker";        docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' 2>&1 | head -20
CT="$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null | awk 'tolower($2) ~ /caddy/ {print $1; exit}')"
if [ -n "$CT" ]; then
  h "Caddy em Docker: $CT"
  docker inspect -f 'rede={{.HostConfig.NetworkMode}} {{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}' "$CT"
  docker exec "$CT" sh -c 'grep -n -A3 -E "'"$DOMAIN"'|ch8ai|^:80|^http://" /etc/caddy/Caddyfile' 2>&1 | head -40
  docker logs --tail 20 "$CT" 2>&1 | grep -i -E "newsloop|error|tls" | tail -10
fi
h "Outros proxies"; ss -ltnp 2>/dev/null | grep -E ':(80|443) ' 
h "DNS";            getent hosts "$DOMAIN"
h "Externo";        curl -sS -m 10 -o /dev/null -w "http %{http_code}\n" "http://$DOMAIN/saude" 2>&1; curl -sS -m 10 -o /dev/null -w "https %{http_code}\n" "https://$DOMAIN/saude" 2>&1

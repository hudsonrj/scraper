#!/usr/bin/env bash
# Instala ou atualiza o newsloop num servidor Ubuntu/Debian. Rodar como root:
#
#   curl -fsSL https://raw.githubusercontent.com/hudsonrj/scraper/claude/festive-planck-2n2ryb/deploy/install.sh | bash
#
# Pode rodar de novo para atualizar: o código é atualizado e os dados ficam.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/hudsonrj/scraper.git}"
BRANCH="${BRANCH:-claude/festive-planck-2n2ryb}"
APP_DIR="${APP_DIR:-/root/apps/harnessend}"
DATA_DIR="${DATA_DIR:-$APP_DIR/data}"
DOMAIN="${DOMAIN:-newsloop.ch8ai.com.br}"
PORT="${PORT:-}"

say(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Rode como root."; exit 1; }

say "Pacotes do sistema"
need=()
command -v git >/dev/null || need+=(git)
python3 -c 'import venv, ensurepip' 2>/dev/null || need+=(python3-venv)
command -v ss >/dev/null || need+=(iproute2)
if [ ${#need[@]} -gt 0 ]; then apt-get update -qq && apt-get install -y -qq "${need[@]}"; fi

say "Código em $APP_DIR (branch $BRANCH)"
mkdir -p "$(dirname "$APP_DIR")"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch -q origin "$BRANCH"
  git -C "$APP_DIR" checkout -q "$BRANCH"
  git -C "$APP_DIR" reset -q --hard "origin/$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

say "Dados em $DATA_DIR (fora do git: revisões e execuções nunca são sobrescritas)"
if [ ! -f "$DATA_DIR/exam/cases.jsonl" ]; then
  mkdir -p "$DATA_DIR"
  cp -rn "$APP_DIR/harness/." "$DATA_DIR/"
fi
mkdir -p "$DATA_DIR/runs" "$DATA_DIR/.cache"

say "Ambiente Python"
[ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/deploy/requirements.txt"
"$APP_DIR/.venv/bin/python" -c "import sys; sys.path.insert(0,'$APP_DIR/src'); from harness_loop.taxonomy import Taxonomy; print('árvore temática:', len(Taxonomy.load().labels), 'temas')"

say "Configuração (.env)"
ENV="$APP_DIR/.env"
if [ ! -f "$ENV" ]; then
  SENHA="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' )"
  cat > "$ENV" <<EOT
HARNESS_DIR=$DATA_DIR
PAINEL_USUARIO=admin
PAINEL_SENHA=$SENHA
# Chaves: o instalador copia de outras aplicações do servidor quando encontra.
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GROQ_API_KEY=
BIG_MODEL=gpt-4o-mini
EDITOR_MODEL=gpt-4o-mini
EOT
  chmod 600 "$ENV"
  echo "Senha do /revisar criada: usuário admin, senha $SENHA (guardada em $ENV)"
else
  grep -q '^HARNESS_DIR=' "$ENV" || echo "HARNESS_DIR=$DATA_DIR" >> "$ENV"
  echo "$ENV já existe; mantido."
fi

say "Chaves de API reaproveitadas de outras aplicações"
# Procura cada chave nos .env das outras aplicações do servidor e copia a primeira
# encontrada. Só mostra de onde veio, nunca o valor.
env_get(){ grep -E "^(export +)?$1=" "$2" 2>/dev/null | tail -1 | sed -E "s/^(export +)?$1=//; s/^[\"']//; s/[\"'] *$//; s/ +#.*$//"; }
env_set(){
  if grep -qE "^$1=" "$ENV"; then
    python3 - "$ENV" "$1" "$2" <<'PY'
import sys
path, key, val = sys.argv[1:]
lines = open(path).read().splitlines()
out = [f"{key}={val}" if l.startswith(key + "=") else l for l in lines]
open(path, "w").write("\n".join(out) + "\n")
PY
  else
    echo "$1=$2" >> "$ENV"
  fi
}
found_any=0
for KEY in OPENAI_API_KEY ANTHROPIC_API_KEY GROQ_API_KEY; do
  if [ -n "$(env_get "$KEY" "$ENV")" ]; then echo "$KEY: já definida no .env do newsloop"; found_any=1; continue; fi
  for f in /root/apps/*/.env /root/apps/*/*/.env /root/*/.env /opt/*/.env /srv/*/.env /var/www/*/.env /etc/environment; do
    [ -f "$f" ] || continue
    case "$f" in "$APP_DIR"/*) continue;; esac
    v="$(env_get "$KEY" "$f")"
    if [ -n "$v" ]; then env_set "$KEY" "$v"; echo "$KEY: copiada de $f"; found_any=1; break; fi
  done
done
[ "$found_any" = 1 ] || echo "Nenhuma chave encontrada. Preencha ANTHROPIC_API_KEY ou OPENAI_API_KEY em $ENV para rodar o loop."
chmod 600 "$ENV"

# Editor pequeno local: se o Ollama estiver rodando com um modelo pequeno, usa ele.
if [ -z "$(env_get EDITOR_BASE_URL "$ENV")" ] && command -v curl >/dev/null; then
  tags="$(curl -fsS -m 3 http://127.0.0.1:11434/api/tags 2>/dev/null || true)"
  if [ -n "$tags" ]; then
    small="$(printf '%s' "$tags" | python3 -c '
import json,sys,re
names=[m["name"] for m in json.load(sys.stdin).get("models",[])]
pick=[n for n in names if re.search(r"(^|[:\-_])(1\.5|3|4|7|8)b", n)]
pref=[n for n in pick if "qwen" in n] or pick
print(pref[0] if pref else "")' 2>/dev/null || true)"
    if [ -n "$small" ]; then
      env_set EDITOR_BASE_URL "http://127.0.0.1:11434/v1"
      env_set EDITOR_MODEL "$small"
      echo "Editor: Ollama local com $small (o modelo pequeno do artigo)"
    else
      echo "Ollama encontrado, mas sem modelo pequeno (ex.: ollama pull qwen3:4b); o editor usa a API."
    fi
  fi
fi
# Sem modelo pequeno local, mas com chave da Groq: editor gpt-oss-20b (~3,6B ativos), na Groq.
if [ -z "$(env_get EDITOR_BASE_URL "$ENV")" ] && [ -n "$(env_get GROQ_API_KEY "$ENV")" ]; then
  env_set EDITOR_BASE_URL "https://api.groq.com/openai/v1"
  env_set EDITOR_API_KEY "$(env_get GROQ_API_KEY "$ENV")"
  env_set EDITOR_MODEL "openai/gpt-oss-20b"
  echo "Editor: openai/gpt-oss-20b na Groq"
fi
# Os modelos padrão dependem da chave: Claude (claude-opus-5-5 congelado, claude-haiku-5-5 editor)
# ou OpenAI (gpt-4o-mini). Remove o padrão antigo da OpenAI se só existe chave da Anthropic.
if [ -n "$(env_get ANTHROPIC_API_KEY "$ENV")" ] && [ -z "$(env_get OPENAI_API_KEY "$ENV")" ]; then
  sed -i -E '/^(BIG|EDITOR)_MODEL=gpt-/d' "$ENV"
fi

say "Porta interna"
UNIT=/etc/systemd/system/newsloop-web.service
if [ -z "$PORT" ] && [ -f "$UNIT" ]; then PORT="$(grep -oP '127\.0\.0\.1:\K[0-9]+' "$UNIT" | head -1)"; fi
if [ -z "$PORT" ]; then
  for p in 8090 8091 8092 8093 8094 8095 8190 8290; do
    ss -ltn "( sport = :$p )" | grep -q LISTEN || { PORT=$p; break; }
  done
fi
[ -n "$PORT" ] || { echo "Nenhuma porta livre na lista; rode com PORT=xxxx"; exit 1; }
echo "porta $PORT"

say "Onde está o proxy (Caddy no host, Caddy em Docker ou nginx)"
PROXY=""; CADDYFILE=""; CADDY_CT=""; UPSTREAM_HOST="127.0.0.1"; EXTRA_BIND=""
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  CADDY_CT="$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /caddy/ {print $1; exit}')"
fi
if [ -n "$CADDY_CT" ]; then
  PROXY=caddy-docker
  # arquivo do host montado como /etc/caddy/Caddyfile (ou a pasta /etc/caddy)
  while IFS='|' read -r src dst; do
    if [ "$dst" = "/etc/caddy/Caddyfile" ]; then CADDYFILE="$src"; fi
    if [ -z "$CADDYFILE" ] && [ "$dst" = "/etc/caddy" ] && [ -f "$src/Caddyfile" ]; then CADDYFILE="$src/Caddyfile"; fi
  done < <(docker inspect -f '{{range .Mounts}}{{.Source}}|{{.Destination}}{{"\n"}}{{end}}' "$CADDY_CT")
  NETMODE="$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$CADDY_CT")"
  if [ "$NETMODE" != "host" ]; then
    # o contêiner alcança o host pelo gateway da rede dele
    UPSTREAM_HOST="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' "$CADDY_CT" | awk '{print $1}')"
    EXTRA_BIND="--bind $UPSTREAM_HOST:$PORT"
  fi
  echo "Caddy em Docker: contêiner $CADDY_CT, rede $NETMODE, Caddyfile ${CADDYFILE:-não encontrado}, upstream $UPSTREAM_HOST:$PORT"
elif command -v caddy >/dev/null && [ -f /etc/caddy/Caddyfile ]; then
  PROXY=caddy-host; CADDYFILE=/etc/caddy/Caddyfile
  echo "Caddy no host: $CADDYFILE"
elif command -v nginx >/dev/null; then
  PROXY=nginx; echo "nginx no host"
else
  echo "Nenhum proxy encontrado."
fi

say "Serviço systemd newsloop-web"
cat > "$UNIT" <<EOT
[Unit]
Description=newsloop: painel do loop de harness
After=network.target docker.service

[Service]
WorkingDirectory=$APP_DIR/deploy/web
EnvironmentFile=$ENV
ExecStart=$APP_DIR/.venv/bin/gunicorn --workers 1 --threads 4 --bind 127.0.0.1:$PORT $EXTRA_BIND --access-logfile - app:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOT

cat > /etc/systemd/system/newsloop-loop.service <<EOT
[Unit]
Description=newsloop: uma execução do loop de harness

[Service]
Type=oneshot
ExecStart=$APP_DIR/deploy/run-loop.sh --rounds 4 --candidates 8 --workers 4 --max-calls 6000
EOT
cat > /etc/systemd/system/newsloop-loop.timer <<EOT
[Unit]
Description=newsloop: loop semanal (desligado até você ativar)

[Timer]
OnCalendar=Sun 03:17
Persistent=true

[Install]
WantedBy=timers.target
EOT
chmod +x "$APP_DIR/deploy/run-loop.sh"
systemctl daemon-reload
systemctl enable -q --now newsloop-web
systemctl restart newsloop-web
sleep 3
curl -fsS "http://127.0.0.1:$PORT/saude" >/dev/null && echo "app respondendo em 127.0.0.1:$PORT" || { journalctl -u newsloop-web -n 30 --no-pager; exit 1; }
if [ -n "$EXTRA_BIND" ] && command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow from 172.16.0.0/12 to any port "$PORT" proto tcp >/dev/null && echo "ufw: porta $PORT liberada só para redes Docker"
fi

say "Proxy reverso para $DOMAIN"
add_caddy_block(){
  local f="$1"
  if grep -qE "^[[:space:]]*(https?://)?$DOMAIN([[:space:],{]|$)" "$f"; then
    sed -i -E "/^[[:space:]]*(https?:\/\/)?${DOMAIN//./\\.}/,/^}/ s#reverse_proxy .*#reverse_proxy $UPSTREAM_HOST:$PORT#" "$f"
    echo "Bloco $DOMAIN já existia em $f: upstream atualizado"
  else
    cp "$f" "$f.bak.$(date +%s)"
    printf '\n%s {\n\treverse_proxy %s:%s\n}\n' "$DOMAIN" "$UPSTREAM_HOST" "$PORT" >> "$f"
    echo "Bloco $DOMAIN adicionado em $f (cópia de segurança ao lado)"
  fi
}
case "$PROXY" in
  caddy-host)
    add_caddy_block "$CADDYFILE"
    caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null
    systemctl reload caddy
    ;;
  caddy-docker)
    if [ -z "$CADDYFILE" ]; then
      echo "Não achei o Caddyfile montado no contêiner $CADDY_CT. Adicione manualmente:"
      printf '\n%s {\n\treverse_proxy %s:%s\n}\n' "$DOMAIN" "$UPSTREAM_HOST" "$PORT"
      exit 1
    fi
    add_caddy_block "$CADDYFILE"
    docker exec "$CADDY_CT" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
    docker exec "$CADDY_CT" caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
    ;;
  nginx)
    cat > /etc/nginx/sites-available/newsloop <<EOT
server {
  listen 80;
  server_name $DOMAIN;
  location / { proxy_pass http://127.0.0.1:$PORT; proxy_set_header Host \$host; proxy_set_header X-Forwarded-Proto \$scheme; }
}
EOT
    ln -sf /etc/nginx/sites-available/newsloop /etc/nginx/sites-enabled/newsloop
    nginx -t && systemctl reload nginx
    command -v certbot >/dev/null && certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email || echo "Para HTTPS: certbot --nginx -d $DOMAIN"
    ;;
  *)
    echo "O app está em 127.0.0.1:$PORT; configure o proxy para $DOMAIN."
    ;;
esac

say "Verificação"
sleep 5
if curl -fsS -m 20 "https://$DOMAIN/saude" 2>/dev/null | grep -q '"ok"'; then
  echo "OK: https://$DOMAIN responde com o newsloop"
else
  echo "Ainda não responde por HTTPS. O certificado pode levar até 1 minuto."
  echo "Rode depois: bash $APP_DIR/deploy/diagnose.sh"
fi

say "Pronto"
echo "Site:     https://$DOMAIN"
echo "Painel:   https://$DOMAIN/painel"
echo "Revisão:  https://$DOMAIN/revisar  (usuário e senha em $ENV)"
echo "Loop:     preencha OPENAI_API_KEY em $ENV e rode $APP_DIR/deploy/run-loop.sh --rounds 2"
echo "Semanal:  systemctl enable --now newsloop-loop.timer"

#!/usr/bin/env bash
# One command to bring the whole stack up on a new machine.
#
#   scripts/bootstrap.sh [--host <name-or-ip>] [--no-build]
#
# --host   What a browser on another machine uses to reach this one (the web UI
#          calls the API at http://<host>:3000). Default: localhost.
# --no-build  Reuse images already built.
#
# What it does, in order:
#   1. checks the tools and the disk;
#   2. fetches the parser submodule;
#   3. writes .env with fresh secrets if there is none (an existing .env is kept);
#   4. starts Postgres, creates the roles and applies the migrations;
#   5. starts everything and waits until it is healthy;
#   6. checks each endpoint.
#
# Secrets you must supply yourself are listed at the end if they are empty:
# OpenRouter, VoiceStudio and (for downloading) a Douyin cookie.
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=localhost
BUILD=--build
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="${2:?--host needs a value}"; shift 2 ;;
    --no-build) BUILD=""; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s\n' "$*"; }
die() { echo "error: $*" >&2; exit 1; }

# --- 1. tools -------------------------------------------------------------------
say "checking tools"
for t in git docker curl openssl awk; do
  command -v "$t" >/dev/null 2>&1 || die "$t is not installed"
done
docker compose version >/dev/null 2>&1 || die "docker compose v2 is not available"
docker info >/dev/null 2>&1 || die "the Docker daemon is not running"

free_gb=$(df -Pk . | awk 'NR==2 {printf "%d", $4/1024/1024}')
[ "$free_gb" -ge 30 ] || echo "warning: only ${free_gb} GB free here; the images, Demucs models and media need about 30 GB"
if [ "$(uname)" = "Linux" ]; then
  mem_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  [ "$mem_gb" -ge 8 ] || echo "warning: ${mem_gb} GB of RAM; ClickHouse (2 GB) and Demucs want at least 8 GB"
fi

# --- 2. submodule ---------------------------------------------------------------
say "fetching the parser submodule"
git submodule update --init --recursive

# --- 3. .env ---------------------------------------------------------------------
# set_kv KEY VALUE: replaces the first KEY= line, or appends one. The value goes through
# the environment so no character in it needs escaping.
set_kv() {
  local key="$1" value="$2" tmp
  tmp=$(mktemp)
  KV_KEY="$key" KV_VALUE="$value" awk '
    BEGIN { k = ENVIRON["KV_KEY"]; v = ENVIRON["KV_VALUE"]; done = 0 }
    index($0, k "=") == 1 && !done { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' .env > "$tmp" && mv "$tmp" .env
}
get_kv() { awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' .env; }
rand() { openssl rand -hex "${1:-24}"; }

say "preparing .env"
if [ -f .env ]; then
  echo ".env exists; keeping it."
else
  cp .env.example .env
  chmod 600 .env
  pg_pw=$(rand); loc_pw=$(rand)
  set_kv API_KEY "$(rand)"
  set_kv POSTGRES_PASSWORD "$pg_pw"
  set_kv DATABASE_URL "postgres://reup:${pg_pw}@postgres:5432/reup"
  set_kv REDIS_URL "redis://redis:6379"
  set_kv PARSER_BASE_URL "http://parser:80"
  set_kv TEMPORAL_DB_PASSWORD "$(rand)"
  set_kv LANGFUSE_DB_PASSWORD "$(rand)"
  set_kv LOCALIZATION_DB_PASSWORD "$loc_pw"
  set_kv LOC_DATABASE_URL "postgresql://localization:${loc_pw}@postgres:5432/reup"
  set_kv MINIO_ROOT_USER "reupadmin"
  set_kv MINIO_ROOT_PASSWORD "$(rand)"
  set_kv STORAGE_BACKEND "s3"
  set_kv S3_SECRET_ACCESS_KEY "$(rand)"
  set_kv LOC_S3_SECRET_ACCESS_KEY "$(rand)"
  set_kv LANGFUSE_S3_SECRET_ACCESS_KEY "$(rand)"
  set_kv CLICKHOUSE_PASSWORD "$(rand)"
  set_kv NEXTAUTH_SECRET "$(rand 32)"
  set_kv SALT "$(rand 32)"
  set_kv ENCRYPTION_KEY "$(rand 32)"
  set_kv LANGFUSE_REDIS_PASSWORD "$(rand)"
  set_kv LANGFUSE_INIT_USER_EMAIL "operator@reup.local"
  set_kv LANGFUSE_INIT_USER_PASSWORD "$(rand 12)"
  set_kv LANGFUSE_PUBLIC_KEY "pk-lf-$(rand 16)"
  set_kv LANGFUSE_SECRET_KEY "sk-lf-$(rand 16)"
  set_kv DUBBER_MODE "code"
  echo "wrote .env with new secrets (mode 600). Fill in the empty provider values, see the end."
fi
# What a browser elsewhere uses; harmless for localhost.
set_kv CORS_ALLOWED_ORIGINS "http://localhost:5173,http://${HOST}:5173"
set_kv VITE_API_BASE_URL "http://${HOST}:3300"

# --- 4. database -----------------------------------------------------------------
say "starting Postgres, creating roles, applying migrations"
docker compose up -d postgres
docker compose run --rm db-init
scripts/apply-migrations.sh
echo "Review schema is additive; starter delivery defaults to legacy_render."
echo "Review profile/canary activation requires docs/hyperframes-review.md gates; bootstrap does not enable it."

# --- 5. everything ---------------------------------------------------------------
say "building and starting the stack (the first build downloads a lot)"
# shellcheck disable=SC2086
docker compose up -d $BUILD

wait_healthy() { # service, seconds
  local svc="$1" limit="$2" waited=0 id status
  while [ "$waited" -lt "$limit" ]; do
    id=$(docker compose ps -q "$svc" 2>/dev/null || true)
    if [ -n "$id" ]; then
      status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$id")
      case "$status" in healthy|running) echo "ok    $svc"; return 0 ;; esac
    fi
    sleep 5; waited=$((waited + 5))
  done
  echo "FAIL  $svc did not become healthy in ${limit}s; see: docker compose logs $svc" >&2
  return 1
}

say "waiting for services"
failed=0
for svc in postgres redis parser nest temporal langfuse-web langfuse-worker web \
           localization-starter worker-agents worker-media; do
  wait_healthy "$svc" 600 || failed=1
done

# --- 6. endpoints ----------------------------------------------------------------
say "checking endpoints"
api_key=$(get_kv API_KEY)
check() { # label, expected, curl args...
  local label="$1" expect="$2"; shift 2
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true)
  if [ "$code" = "$expect" ]; then echo "ok    $label ($code)"; else echo "FAIL  $label (got $code, want $expect)" >&2; failed=1; fi
}
check "API health"              200 http://localhost:3300/health
check "API rejects no key"      401 http://localhost:3300/api/workspace/snapshot
check "API with key"            200 -H "X-Api-Key: ${api_key}" http://localhost:3300/api/workspace/snapshot
check "web UI"                  200 http://localhost:5173/
check "Langfuse"                200 http://localhost:3100/api/public/health
check "Temporal UI"             200 http://localhost:8233/

# --- summary ---------------------------------------------------------------------
say "values still empty in .env (needed for a real video)"
missing=0
for k in OPENROUTER_API_KEY LOCALIZER_MODEL DUBBER_MODEL OCR_MODEL OCR_BBOX_FORMAT REVIEWER_MODEL \
         VOICESTUDIO_URL VOICESTUDIO_API_KEY VOICE_ID STT_MODEL TTS_MODEL; do
  if [ -z "$(get_kv "$k")" ]; then echo "  $k"; missing=1; fi
done
[ "$missing" -eq 0 ] && echo "  none"
echo
echo "A Douyin cookie is also needed to download: push it with the Chrome extension"
echo "(services/extension) or set DOUYIN_COOKIE_FALLBACK in .env."
echo
echo "Web UI:      http://${HOST}:5173      API key: the API_KEY in .env"
echo "Langfuse:    http://localhost:3100    login: $(get_kv LANGFUSE_INIT_USER_EMAIL) / LANGFUSE_INIT_USER_PASSWORD in .env"
echo "Temporal UI: http://localhost:8233"
echo "After editing .env: docker compose up -d   (recreates what changed)"
if [ "$failed" -ne 0 ]; then echo; echo "Some checks failed, see above." >&2; exit 1; fi
echo; echo "Stack is up. Next: scripts/e2e-smoke.sh <douyin video url>"

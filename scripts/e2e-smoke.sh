#!/usr/bin/env bash
# Runs one real video through the whole pipeline and reports how it ended.
#
#   scripts/e2e-smoke.sh <douyin video page url> [timeout-minutes]
#
# It queues the video through the API (POST /api/videos), then watches
# localization.runs until the run reaches a final status. Exit 0 only for
# `published`; every other final status exits 1 and says which.
#
# Needs: the stack up (scripts/bootstrap.sh), the provider values in .env, and a
# Douyin cookie the parser can use (Chrome extension push or DOUYIN_COOKIE_FALLBACK).
set -euo pipefail
cd "$(dirname "$0")/.."

URL="${1:?usage: e2e-smoke.sh <douyin video page url> [timeout-minutes]}"
LIMIT_MIN="${2:-30}"

get_kv() { awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' .env; }
API_KEY=$(get_kv API_KEY)
[ -n "$API_KEY" ] || { echo "API_KEY is empty in .env" >&2; exit 2; }

psql_() {
  docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAt -c "$1"' sh "$1"
}

echo "queueing $URL"
resp=$(curl -s --max-time 60 -w '\n%{http_code}' -X POST http://localhost:3000/api/videos \
  -H "X-Api-Key: ${API_KEY}" -H 'Content-Type: application/json' \
  -d "$(printf '{"url":"%s"}' "$URL")")
code=${resp##*$'\n'}; body=${resp%$'\n'*}
if [ "$code" != "202" ]; then echo "enqueue failed: HTTP $code: $body" >&2; exit 2; fi
aweme_id=$(printf '%s' "$body" | sed -n 's/.*"aweme_id":"\([0-9]*\)".*/\1/p')
[ -n "$aweme_id" ] || { echo "no aweme_id in: $body" >&2; exit 2; }
echo "aweme_id=$aweme_id"

start=$(date +%s)
last=""
while :; do
  elapsed=$(( $(date +%s) - start ))
  [ "$elapsed" -le $(( LIMIT_MIN * 60 )) ] || { echo "timed out after ${LIMIT_MIN} min (last: $last)" >&2; exit 3; }

  video=$(psql_ "SELECT status FROM videos WHERE aweme_id = '$aweme_id'" || true)
  run=$(psql_ "SELECT generation || ' ' || status FROM localization.runs WHERE aweme_id = '$aweme_id' ORDER BY generation DESC LIMIT 1" || true)
  state="download=${video:-none} run=${run:-none}"
  [ "$state" = "$last" ] || { printf '[%4ss] %s\n' "$elapsed" "$state"; last="$state"; }

  status=${run#* }
  case "$status" in
    published) echo "OK: published in ${elapsed}s"; exit 0 ;;
    published_original|skipped_ad|skipped_no_video|needs_human|failed)
      echo "ended as: $status (after ${elapsed}s)" >&2; exit 1 ;;
  esac
  sleep 10
done

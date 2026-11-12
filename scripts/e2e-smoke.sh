#!/usr/bin/env bash
# Runs one real video through the whole pipeline and reports how it ended.
#
#   scripts/e2e-smoke.sh <douyin video page url> [timeout-minutes]
#
# It queues the video through the API (POST /api/videos), then watches
# localization.runs until the run reaches a final status. Exit 0 only for
# review_project: ready_for_review plus a valid bundle and no output; needs_human exits 4.
# --mode legacy_render explicitly preserves the published success criterion.
#
# Needs: the stack up (scripts/bootstrap.sh), the provider values in .env, and a
# Douyin cookie the parser can use (Chrome extension push or DOUYIN_COOKIE_FALLBACK).
set -euo pipefail
cd "$(dirname "$0")/.."

MODE=review_project
if [ "${1:-}" = --mode ]; then
  MODE="${2:?mode required}"
  shift 2
fi
case "$MODE" in
  review_project|legacy_render) ;;
  *) echo 'Invalid delivery mode' >&2; exit 2 ;;
esac
if [ "${1:-}" = --run ]; then
  aweme_id="${2:?aweme required}"; generation="${3:?generation required}"; LIMIT_MIN="${4:-30}"
else
  URL="${1:?usage: e2e-smoke.sh [--mode review_project|legacy_render] <url> [minutes] OR --run <aweme> <generation> [minutes]}"
  LIMIT_MIN="${2:-30}"
fi
[[ "$LIMIT_MIN" =~ ^[1-9][0-9]*$ ]] || { echo 'Invalid timeout' >&2; exit 2; }

psql_() {
  docker compose exec -T postgres sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qAt -c "$1"' sh "$1"
}

if [ "${URL:-}" != '' ]; then
  get_kv() { awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' .env; }
  API_KEY=$(get_kv API_KEY)
  [ -n "$API_KEY" ] || { echo "API_KEY is empty in .env" >&2; exit 2; }
  echo "queueing $URL"
  resp=$(printf 'X-Api-Key: %s\n' "$API_KEY" | curl -sS --max-time 60 -w '\n%{http_code}' \
    -X POST http://localhost:3300/api/videos -H @- -H 'Content-Type: application/json' \
    -d "$(printf '{"url":"%s","localize":true}' "$URL")") || { echo 'API unavailable' >&2; exit 2; }
  code=${resp##*$'\n'}
  body=${resp%$'\n'*}
  if [ "$code" != "202" ]; then
    echo "enqueue failed: HTTP $code: $body" >&2
    exit 2
  fi
  aweme_id=$(printf '%s' "$body" | sed -n 's/.*"aweme_id":"\([0-9]*\)".*/\1/p')
  [ -n "$aweme_id" ] || { echo "no aweme_id in: $body" >&2; exit 2; }
  echo "aweme_id=$aweme_id"
  [[ "$aweme_id" =~ ^[0-9]{1,32}$ ]] || { echo 'Invalid aweme_id' >&2; exit 2; }
  generation=$(psql_ "SELECT generation FROM localization_outbox WHERE aweme_id = '$aweme_id'") || { echo 'Database unavailable' >&2; exit 2; }
  [[ "$generation" =~ ^[0-9]{1,7}$ ]] || { echo 'Missing outbox generation' >&2; exit 2; }
fi
[[ "$aweme_id" =~ ^[0-9]{1,32}$ && "$generation" =~ ^[0-9]{1,7}$ ]] || { echo 'Invalid run identity' >&2; exit 2; }
echo "aweme_id=$aweme_id generation=$generation expected=$MODE (existing videos are not reprocessed)"

start=$(date +%s)
last=""
while :; do
  elapsed=$(( $(date +%s) - start ))
  [ "$elapsed" -le $(( LIMIT_MIN * 60 )) ] || { echo "timed out after ${LIMIT_MIN} min (last: $last)" >&2; exit 3; }

  video=$(psql_ "SELECT status FROM videos WHERE aweme_id = '$aweme_id'" || true)
  run=$(psql_ "SELECT generation || ' ' || status FROM localization.runs WHERE aweme_id = '$aweme_id' AND generation = $generation") || { echo 'Database unavailable' >&2; exit 2; }
  state="download=${video:-none} run=${run:-none}"
  [ "$state" = "$last" ] || { printf '[%4ss] %s\n' "$elapsed" "$state"; last="$state"; }

  status=${run#* }
  case "$status" in
    ready_for_review|needs_human)
      if [ "$MODE" = review_project ]; then
        valid=$(psql_ "SELECT output_key IS NULL AND project_key = 'review/$aweme_id/g$generation/head.json' FROM localization.runs WHERE aweme_id = '$aweme_id' AND generation = $generation") || { echo 'Database unavailable' >&2; exit 2; }
        [ "$valid" = t ] || { echo 'Review result is missing its project or has final output' >&2; exit 1; }
        if ! docker compose exec -T worker-agents python -m localization.starter.verify_delivery "$aweme_id" "$generation"; then
          echo 'Review bundle verification failed' >&2; exit 1
        fi
        if [ "$status" = ready_for_review ]; then echo "OK: review project ready in ${elapsed}s"; exit 0; fi
        echo 'Project requires human correction; not published' >&2; exit 4
      fi
      echo "ended as: $status (expected published)" >&2; exit 1 ;;
    published)
      if [ "$MODE" = legacy_render ]; then echo "OK: published in ${elapsed}s"; exit 0; fi
      echo 'Unexpected published result in review mode' >&2; exit 1 ;;
    published_original|skipped_ad|skipped_no_video|failed)
      echo "ended as: $status (after ${elapsed}s)" >&2; exit 1 ;;
  esac
  sleep 10
done

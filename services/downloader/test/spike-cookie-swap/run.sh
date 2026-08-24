#!/usr/bin/env bash
# SPIKE 2 (GATE) -- single command: generate certs, bring the stack up, run
# the jest spike, tear the stack down. Always tears down (trap), even on
# failure, so repeated runs (including mutation runs) start from a clean
# state.
#
# Env knobs used by mutation scripts (see mutations/):
#   SPIKE_COOKIE_KEY      Redis key the JEST TEST writes to (default: the
#                         real key, douyin:cookie:current).
#   SPIKE_REDIS_URL       REDIS_URL given to the PARSER container (default:
#                         the real, reachable spike redis).
#   SPIKE_FALLBACK_COOKIE DOUYIN_COOKIE_FALLBACK given to the parser
#                         container; must match what the test expects.
#   KEEP_ARTIFACTS=1      skip deleting ./certs and ./data on teardown, for
#                         post-mortem inspection.
set -euo pipefail

cd "$(dirname "$0")"
SERVICE_DIR="$(cd ../../ && pwd)"
COMPOSE="docker compose -f docker-compose.spike.yml -p spike-cookie-swap"
PARSER_URL="http://localhost:18080"
REDIS_PORT=16379

cleanup() {
  local status=$?
  echo
  echo "=== tearing down (exit code so far: $status) ==="
  $COMPOSE down -v --remove-orphans || true
  if [ "${KEEP_ARTIFACTS:-0}" != "1" ]; then
    rm -rf ./certs
    # captured.log is evidence for the caller of this script; only the CA
    # private key needs to disappear promptly. Truncate the log so the next
    # run starts clean, but do not delete the directory the compose file
    # bind-mounts (avoids a re-`mkdir` race with the next `up`).
    mkdir -p ./data
    : > ./data/captured.log
  fi
  exit "$status"
}
trap cleanup EXIT

echo "=== [1/5] generating throwaway TLS material ==="
./setup-certs.sh

mkdir -p ./data
: > ./data/captured.log

echo "=== [2/5] docker compose up (build parser fresh from the untouched fork) ==="
echo "REDIS_URL override for parser: ${SPIKE_REDIS_URL:-<default, reachable>}"
echo "DOUYIN_COOKIE_FALLBACK for parser: ${SPIKE_FALLBACK_COOKIE:-spike_fallback_sentinel_C}"
$COMPOSE up -d --build

echo "=== [3/5] waiting for parser + redis + echo to accept connections ==="
for i in $(seq 1 60); do
  if curl -sf -o /dev/null "$PARSER_URL/docs"; then
    echo "parser is up after ${i}s"
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "FAIL: parser never became reachable on $PARSER_URL"
    $COMPOSE logs parser
    exit 1
  fi
  sleep 1
done

for i in $(seq 1 30); do
  if (echo >"/dev/tcp/127.0.0.1/$REDIS_PORT") >/dev/null 2>&1; then
    echo "redis is up after ${i}s"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "FAIL: redis never became reachable on 127.0.0.1:$REDIS_PORT"
    exit 1
  fi
  sleep 1
done

# `echo` (the fake-Douyin TLS endpoint) is not published to the host --
# only the containers on spikenet can reach it, by design (see
# docker-compose.spike.yml). Probe its listening socket from the INSIDE via
# `docker compose exec`, using python3 (guaranteed present in the
# python:3.11-slim image; no netcat dependency). Without this wait, a slow
# echo container start makes step 1 fail for a completely unrelated reason
# (parser reachable + redis reachable, but the fake Douyin endpoint not
# listening yet).
for i in $(seq 1 30); do
  if $COMPOSE exec -T echo python3 -c \
    "import socket; s = socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', 443))" \
    >/dev/null 2>&1; then
    echo "echo is up after ${i}s"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "FAIL: echo (fake Douyin TLS endpoint) never became reachable"
    $COMPOSE logs echo
    exit 1
  fi
  sleep 1
done

echo "=== [4/5] running the SPIKE 2 jest suite ==="
set +e
(
  cd "$SERVICE_DIR"
  SPIKE_COOKIE_KEY="${SPIKE_COOKIE_KEY:-douyin:cookie:current}" \
  SPIKE_FALLBACK_COOKIE="${SPIKE_FALLBACK_COOKIE:-spike_fallback_sentinel_C}" \
    pnpm exec jest --config test/spike-cookie-swap/jest.spike.config.js --runInBand
)
JEST_EXIT=$?
set -e

echo "=== [5/5] result ==="
if [ "$JEST_EXIT" -eq 0 ]; then
  echo "SPIKE 2: GREEN"
else
  echo "SPIKE 2: RED (exit $JEST_EXIT)"
  echo "--- parser logs (tail) ---"
  $COMPOSE logs --tail=80 parser
fi

exit "$JEST_EXIT"

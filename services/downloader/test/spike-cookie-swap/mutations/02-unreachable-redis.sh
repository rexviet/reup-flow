#!/usr/bin/env bash
# Required mutation 2 (see phase-00-skeleton-spike.md sec 0.5 report).
#
# Points the PARSER container's REDIS_URL at an IP inside the spike bridge
# subnet (172.28.9.0/24) that no container owns, so every Redis call from
# the parser hits socket_connect_timeout=1 and fails. The swap assertions
# (steps 1-3) must go RED (parser can never see A/B, only the fallback), but
# the no-crash / fallback assertion (step 4) and the anti-restart check must
# still hold -- proving cookie_provider's fallback chain survives Redis being
# completely gone.
#
# Applied change (printed for the record): compose env override
# SPIKE_REDIS_URL, substituted into docker-compose.spike.yml's
# `REDIS_URL: ${SPIKE_REDIS_URL:-redis://172.28.9.10:6379/0}`.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== MUTATION 2: applied config change ==="
echo "SPIKE_REDIS_URL=redis://172.28.9.99:6379/0   (unassigned IP in spikenet; parser's Redis client times out)"
echo

export SPIKE_REDIS_URL="redis://172.28.9.99:6379/0"
echo ">>> expect steps 1-3 RED, step 4 (fallback/no-crash) + anti-restart still GREEN"
if ./run.sh; then
  echo "::error::mutation did not turn the gate red -- the gate is vacuous"
  exit 1
fi
echo "confirmed RED as expected"

echo
echo "=== restoring: unset SPIKE_REDIS_URL ==="
unset SPIKE_REDIS_URL
echo ">>> expect all green again"
./run.sh

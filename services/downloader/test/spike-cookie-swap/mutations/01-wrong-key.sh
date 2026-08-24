#!/usr/bin/env bash
# Required mutation 1 (see phase-00-skeleton-spike.md sec 0.5 report).
#
# Makes the jest test write the sentinel to a key the parser never reads
# (douyin:cookie:wrong instead of douyin:cookie:current). If this does NOT
# turn steps 1-3 RED, the swap assertions are not actually tied to the real
# key and the whole gate is meaningless.
#
# Applied change (printed for the record): env var SPIKE_COOKIE_KEY, which
# cookie-swap.spike-test.ts reads to decide which Redis key IT writes to.
# The parser's own code is untouched -- it always reads douyin:cookie:current.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== MUTATION 1: applied config change ==="
echo "SPIKE_COOKIE_KEY=douyin:cookie:wrong   (parser still reads douyin:cookie:current)"
echo

export SPIKE_COOKIE_KEY="douyin:cookie:wrong"
echo ">>> expect steps 1-3 RED, steps 4-5 unaffected"
if ./run.sh; then
  echo "::error::mutation did not turn the gate red -- the gate is vacuous"
  exit 1
fi
echo "confirmed RED as expected"

echo
echo "=== restoring: unset SPIKE_COOKIE_KEY ==="
unset SPIKE_COOKIE_KEY
echo ">>> expect all green again"
./run.sh

#!/usr/bin/env bash
#
# Mutation suite for the localization_outbox atomicity guard
# (test/db/outbox.spec.ts).
#
# The invariant: a `done` video always has an outbox row, so the row and the
# `done` flip commit together. A guard is evidence only if breaking that turns
# it red. The mutation below runs both statements on the pool, one after the
# other, instead of inside one transaction.
#
# Rules this harness follows (same as the other harnesses):
#   1. A mutation whose diff is empty is a hard error, never a pass.
#   2. Positive evidence that tests ran ("Tests: N ... total", N > 0) and that
#      the run finished (bounded by a timeout); exit code alone cannot tell
#      "caught" from "selected nothing".
#   3. The file is restored from a pristine snapshot on ANY exit, and a restore
#      that did not restore is fatal.
#
# Run from services/downloader:  bash test/mutations/phase-12.sh
# Needs the compose stack's Postgres up.

set -u

cd "$(dirname "$0")/../.." || exit 1

REPO=src/db/videos.repository.ts
PATTERN=test/db/outbox.spec
JEST_TIMEOUT_SECONDS=120

SNAP=$(mktemp -d)
cp "$REPO" "$SNAP/repo.ts"
fail=0

restore_all() { cp "$SNAP/repo.ts" "$REPO"; }
on_exit() {
  if ! diff -q "$SNAP/repo.ts" "$REPO" >/dev/null; then
    echo "NOTE: restored from the pristine snapshot"
    restore_all
  fi
  rm -rf "$SNAP"
}
trap on_exit EXIT

# Kills the whole process tree: killing only the pnpm wrapper orphans jest and
# its workers, which keep Postgres connections and per-run schemas alive.
kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill -9 "$pid" 2>/dev/null
}

# run_jest: 0 green, 1 red, 98 timeout, 99 nothing ran.
run_jest() {
  pnpm exec jest "$PATTERN" >"$SNAP/out.log" 2>&1 &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$JEST_TIMEOUT_SECONDS" ]; then
      kill_tree "$pid"; wait "$pid" 2>/dev/null
      return 98
    fi
    sleep 1; waited=$((waited + 1))
  done
  wait "$pid"; local rc=$?
  grep -qE '^Tests: .*[1-9][0-9]* (passed|failed)' "$SNAP/out.log" || return 99
  [ "$rc" -eq 0 ] && return 0
  return 1
}

# Baseline: the unmutated suite must be green, otherwise a red run after the
# mutation says nothing about the mutation (Postgres down, broken setup...).
# Deliberately not worded like the runner's "still green" control line.
echo "BASELINE: unmutated $PATTERN must be green"
run_jest
baseline_rc=$?
if [ "$baseline_rc" -ne 0 ]; then
  echo "ERROR: baseline run is not green (rc=$baseline_rc); refusing to mutate."
  tail -20 "$SNAP/out.log"
  exit 1
fi
echo "BASELINE: green"

# The tests that exist to catch this mutation. A failure elsewhere (setup, a
# dead database) must not count as caught.
MUST_FAIL_TESTS=(
  "keeps the video downloading when the outbox insert fails"
)

echo
echo "=============================================================="
echo "MUTATION: markDone runs the flip and the outbox insert as two separate commits"
echo "  file:  $REPO"
echo "=============================================================="

python3 - <<'EOF'
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
a = "await exec.transaction(async (tx) => {"
b = "      .onConflictDoNothing();\n  });"
if a in s and b in s:
    s = s.replace(a, "await (async (tx: Executor) => {")
    s = s.replace(b, "      .onConflictDoNothing();\n  })(exec);")
    p.write_text(s)
EOF

if diff -q "$SNAP/repo.ts" "$REPO" >/dev/null; then
  echo "ERROR: mutation matched nothing -- the code shape changed and this"
  echo "       mutation no longer applies. Update it; do NOT treat this as a pass."
  fail=1
else
  echo "--- diff actually applied ---"
  diff "$SNAP/repo.ts" "$REPO"
  echo "-----------------------------"
  echo "  tests: $PATTERN (must FAIL)"
  run_jest
  rc=$?
  case $rc in
    1)
      missing=0
      for t in "${MUST_FAIL_TESTS[@]}"; do
        if ! grep -qE "✕ $t" "$SNAP/out.log"; then
          echo "FAIL: suite went red, but not in the test that guards this: $t"
          missing=1
        fi
      done
      if [ "$missing" -eq 0 ]; then
        echo "OK: caught. Failing tests:"
        grep -E '✕' "$SNAP/out.log"
      else
        echo "ERROR: the test pattern $PATTERN went red for an unrelated reason."
        grep -E '✕' "$SNAP/out.log" || tail -10 "$SNAP/out.log"
        fail=1
      fi
      ;;
    0)
      echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
      tail -20 "$SNAP/out.log"
      fail=1
      ;;
    98)
      echo "FAIL: the run timed out after ${JEST_TIMEOUT_SECONDS}s."
      fail=1
      ;;
    *)
      echo "ERROR: the test pattern $PATTERN selected no tests."
      tail -10 "$SNAP/out.log"
      fail=1
      ;;
  esac
  restore_all
fi

if ! diff -q "$SNAP/repo.ts" "$REPO" >/dev/null; then
  echo "FATAL: failed to restore $REPO"
  exit 1
fi

exit "$fail"

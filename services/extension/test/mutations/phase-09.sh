#!/usr/bin/env bash
#
# Phase 9 mutation suite (phase-09-extension.md §9.3/§9.5, plan.md §0
# nguyên tắc 4).
#
# "The suite is green" is evidence the suite ran, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# corresponding tests to go red. Same two rules as the downloader package's
# harnesses (services/downloader/test/mutations/phase-0{1..5}.sh):
#
#   1. Every mutation prints the diff it actually applied. A mutator that
#      matches nothing is a mutation that never ran -- reporting that as
#      "passed" is how a guard silently dies. An empty diff is a hard error
#      here, not a skip.
#   2. Every mutation restores its file from a pristine snapshot taken
#      BEFORE any mutation runs, and the restore is verified byte-for-byte.
#      `git checkout --` is not used: these files are untracked in a new
#      package, and it silently no-ops on untracked paths.
#
# Run from services/extension:  bash test/mutations/phase-09.sh

set -u

cd "$(dirname "$0")/../.." || exit 1

PUSH_COOKIE=lib/push-cookie.js
API_CLIENT=lib/api-client.js
SW=sw.js

BACKUP_DIR=$(mktemp -d)
fail=0

# Pristine snapshot of every file any mutation below touches, taken up
# front -- see harness rule "Luật thứ năm" in plan.md §5: a watchdog kill
# between apply and restore must not leave a mutated file looking pristine
# to the next run.
for f in "$PUSH_COOKIE" "$API_CLIENT" "$SW"; do
  cp "$f" "$BACKUP_DIR/$(basename "$f").pristine"
done

cleanup() {
  local restored_any=0
  for f in "$PUSH_COOKIE" "$API_CLIENT" "$SW"; do
    if ! diff -q "$BACKUP_DIR/$(basename "$f").pristine" "$f" >/dev/null 2>&1; then
      cp "$BACKUP_DIR/$(basename "$f").pristine" "$f"
      restored_any=1
    fi
  done
  if [ "$restored_any" -eq 1 ]; then
    echo "(cleanup) restored mutated file(s) to their pristine snapshot"
  fi
  rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT INT TERM

backup() { cp "$1" "$BACKUP_DIR/$(basename "$1").bak"; }
restore() {
  cp "$BACKUP_DIR/$(basename "$1").pristine" "$1"
  diff -q "$BACKUP_DIR/$(basename "$1").pristine" "$1" >/dev/null \
    || { echo "FATAL: failed to restore $1"; exit 1; }
}

# Portable stand-in for GNU `timeout` (not installed by default on macOS,
# and this harness must run on the same machine the rest of the phase runs
# on). plan.md §5 "luật thứ sáu": a mutation can HANG instead of failing --
# that must be its own outcome, never silently indistinguishable from a
# caught failure.
run_with_timeout() {
  local secs=$1 logfile=$2
  shift 2
  "$@" >"$logfile" 2>&1 &
  local cmd_pid=$!
  ( sleep "$secs"; kill -9 "$cmd_pid" 2>/dev/null ) &
  local watchdog_pid=$!
  wait "$cmd_pid" 2>/dev/null
  local status=$?
  kill "$watchdog_pid" 2>/dev/null
  wait "$watchdog_pid" 2>/dev/null
  if [ "$status" -eq 137 ] || [ "$status" -eq 143 ]; then
    return 124
  fi
  return "$status"
}

# run_mutation <label> <file> <test-pattern> <mutator-command...>
#
# Applies the mutation, refuses to continue if it changed nothing, requires
# the named tests to fail, then restores from the pristine snapshot.
run_mutation() {
  local label=$1 file=$2 pattern=$3
  shift 3
  echo
  echo "=============================================================="
  echo "MUTATION: $label"
  echo "  file:  $file"
  echo "  tests: $pattern"
  echo "=============================================================="
  backup "$file"

  "$@"

  if diff -q "$BACKUP_DIR/$(basename "$file").bak" "$file" >/dev/null; then
    echo "ERROR: mutation matched nothing -- the code shape changed and this"
    echo "       mutation no longer applies. Update it; do NOT treat this as a pass."
    fail=1
    restore "$file"
    return
  fi

  echo "--- diff actually applied ---"
  diff "$BACKUP_DIR/$(basename "$file").bak" "$file"
  echo "-----------------------------"

  run_with_timeout 60 "$BACKUP_DIR/out.log" pnpm test -- "$pattern"
  status=$?
  if grep -q "No tests found" "$BACKUP_DIR/out.log" 2>/dev/null; then
    echo "ERROR: pattern '$pattern' matched ZERO tests -- this is a hard error,"
    echo "       not evidence of anything caught. Fix the pattern."
    fail=1
    restore "$file"
    return
  fi
  if [ "$status" -eq 0 ]; then
    echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
    tail -30 "$BACKUP_DIR/out.log"
    fail=1
  elif [ "$status" -eq 124 ]; then
    echo "FAIL: test run HUNG (timeout) -- not a caught failure, a separate outcome."
    fail=1
  else
    echo "OK: caught. Failing tests:"
    grep -E '✕|FAIL' "$BACKUP_DIR/out.log" || tail -15 "$BACKUP_DIR/out.log"
  fi

  restore "$file"
}

# --- phase-09 §9.3: "bỏ qua khi cookies.length === 0" -------------------
# Removing this guard lets an empty (logged-out) cookie read overwrite a
# good cookie already on the backend with an effectively-empty one. Caught
# as an OUTCOME (fake backend's stored cookie changes), not merely "fetch
# was not called" -- see test/push-cookie.spec.js's guard test.
run_mutation "disable the sessionid_ss guard entirely" "$PUSH_COOKIE" test/push-cookie.spec.js \
  perl -0pi -e "s/if \(!hasSessionCookie\) \{/if (false) {/" "$PUSH_COOKIE"

# --- code-review should-fix: length-only check misses "non-empty but no
# sessionid_ss" (stale consent/locale cookies survive logout). Regressing
# to the old, weaker check must turn ONLY that specific test red, while the
# plain-empty-list test stays caught by the (still-present) Array.isArray
# check -- proving the two tests are testing two different things, not one.
run_mutation "weaken the guard back to a plain length check (drop the sessionid_ss check)" \
  "$PUSH_COOKIE" test/push-cookie.spec.js \
  perl -0pi -e "s/const hasSessionCookie =\n      Array\.isArray\(cookies\) && cookies\.some\(\(c\) => c && c\.name === 'sessionid_ss'\);/const hasSessionCookie = Array.isArray(cookies) \&\& cookies.length > 0;/" "$PUSH_COOKIE"

# --- phase-09 §9.5 / plan.md §0 nguyên tắc 3: "không console.log cookie" --
# Any console.* call anywhere in production code must fail this check --
# not just ones that happen to reference a cookie-named variable.
run_mutation "add a console.log of the cookie header" "$PUSH_COOKIE" test/no-console-log.spec.js \
  perl -0pi -e "s/(const cookieHeader = root\.buildCookieHeader\(cookies\);)/\$1\n    console.log('debug cookieHeader', cookieHeader);/" "$PUSH_COOKIE"

# --- phase-09 DoD: "Sai API key -> options page hiện lỗi rõ" -------------
# Neutering assertOk() makes pushCookie() resolve as success even when the
# backend answered 401 -- the exact silent failure the DoD guards against.
# Caught by the exact-message assertion in push-cookie.spec.js AND by
# api-client.spec.js's own 401/400 rejection tests.
run_mutation "make assertOk a no-op (401 no longer surfaces as an error)" \
  "$API_CLIENT" test/push-cookie.spec.js \
  perl -0pi -e "s/async function assertOk\(response\) \{/async function assertOk(response) { return;/" "$API_CLIENT"

run_mutation "make assertOk a no-op, checked again from api-client.spec.js directly" \
  "$API_CLIENT" test/api-client.spec.js \
  perl -0pi -e "s/async function assertOk\(response\) \{/async function assertOk(response) { return;/" "$API_CLIENT"

echo
if [ "$fail" -eq 0 ]; then
  echo "All phase-9 mutations applied and were caught."
else
  echo "PHASE 9 MUTATION SUITE FAILED -- see above."
fi
exit "$fail"

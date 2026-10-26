#!/usr/bin/env bash
#
# Tests for scripts/run-mutations.sh itself: selection, and the guards that make a
# hollow or leaky harness a build failure. Runs the REAL runner against throwaway
# harnesses in a sandbox copy of the repo layout, so no real harness is executed.
#
#   bash scripts/test-run-mutations.sh
#
# Every guard is tested the way the guards are meant to be trusted: break the
# thing it watches and require red. In particular, guard D (tree unchanged) must
# go red for a harness that leaves a .py, .tsx, .json or .ttf mutation behind.

set -u

REAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0
CASES=0

fail() { echo "  FAIL: $*"; FAILURES=$((FAILURES + 1)); }

# new_sandbox "<EXPECT rows, one per line>" -> sets SANDBOX
new_sandbox() {
  SANDBOX=$(mktemp -d)
  mkdir -p "$SANDBOX/scripts" \
    "$SANDBOX/services/downloader/test/mutations" \
    "$SANDBOX/services/downloader/src" \
    "$SANDBOX/services/localization/tests/mutations" \
    "$SANDBOX/services/localization/src" \
    "$SANDBOX/services/renderer/src" \
    "$SANDBOX/contracts" "$SANDBOX/assets/fonts"
  REAL_ROOT="$REAL_ROOT" EXPECT_ROWS="$1" SANDBOX="$SANDBOX" python3 - <<'PY'
import os, pathlib, re
src = pathlib.Path(os.environ["REAL_ROOT"], "scripts/run-mutations.sh").read_text()
rows = os.environ["EXPECT_ROWS"]
new, n = re.subn(r'EXPECT="\n.*?\n"\n', 'EXPECT="\n' + rows + '\n"\n', src, count=1, flags=re.S)
assert n == 1, "could not find the EXPECT block"
out = pathlib.Path(os.environ["SANDBOX"], "scripts/run-mutations.sh")
out.write_text(new)
PY
  # Seed one tracked file of every kind the fingerprint must cover.
  echo "x = 1" >"$SANDBOX/services/localization/src/mod.py"
  echo "export const a = 1" >"$SANDBOX/services/renderer/src/view.tsx"
  echo '{}' >"$SANDBOX/contracts/schema.json"
  echo "font" >"$SANDBOX/assets/fonts/face.ttf"
  echo "export const b = 1" >"$SANDBOX/services/downloader/src/thing.ts"
}

# harness DIR PHASE "<body>": a throwaway harness. The body runs from the sandbox root.
harness() {
  local dir=$1 phase=$2 body=$3 path
  case "$dir" in
    downloader) path="$SANDBOX/services/downloader/test/mutations/phase-$phase.sh" ;;
    localization) path="$SANDBOX/services/localization/tests/mutations/phase-$phase.sh" ;;
  esac
  printf '#!/usr/bin/env bash\ncd "%s" || exit 1\necho "$0" >> "%s/ran.log"\n%s\n' \
    "$SANDBOX" "$SANDBOX" "$body" >"$path"
}

caught() { # N lines of caught outcomes
  local n=$1 i out=""
  for i in $(seq 1 "$n"); do out="${out}echo 'OK: caught. Failing tests:'; "; done
  printf '%s' "$out"
}

green() {
  local n=$1 i out=""
  for i in $(seq 1 "$n"); do out="${out}echo 'OK: still green, as required.'; "; done
  printf '%s' "$out"
}

# run_runner ARGS... -> sets RC and OUT
run_runner() {
  : >"$SANDBOX/ran.log"
  OUT=$(bash "$SANDBOX/scripts/run-mutations.sh" "$@" 2>&1)
  RC=$?
}

ran() { # which harness scripts ran, as dir/phase tokens
  sed -E 's#(.*/)?services/([a-z]+)/tests?/mutations/phase-([^.]+)\.sh#\2/\3#' "$SANDBOX/ran.log" | sort | tr '\n' ' ' | sed 's/ $//'
}

case_start() { CASES=$((CASES + 1)); echo "case: $1"; }

expect_rc() {
  [ "$RC" -eq "$1" ] || { fail "expected exit $1, got $RC"; echo "$OUT" | tail -25 | sed 's/^/    | /'; }
}
expect_ran() { [ "$(ran)" = "$1" ] || fail "expected to run [$1], ran [$(ran)]"; }
expect_out() { grep -qE "$1" <<<"$OUT" || fail "output lacks /$1/"; }

ROWS=$'downloader:07:2:1\ndownloader:11:1:0\nlocalization:03:3:0'

good_harnesses() {
  harness downloader 07 "$(caught 2)$(green 1)"
  harness downloader 11 "$(caught 1)"
  harness localization 03 "$(caught 3)"
}

# --- selection ---------------------------------------------------------------

new_sandbox "$ROWS"; good_harnesses
case_start "no selector runs every harness"
run_runner
expect_rc 0; expect_ran "downloader/07 downloader/11 localization/03"

case_start "a bare phase means the downloader's, and only that one"
run_runner 07
expect_rc 0; expect_ran "downloader/07"
run_runner 11
expect_rc 0; expect_ran "downloader/11"

case_start "bare 03 does not select localization:03 (it never did)"
run_runner 03
expect_rc 1; expect_ran ""; expect_out "no harness ran"

case_start "dir/phase selects one harness of another service"
run_runner localization/03
expect_rc 0; expect_ran "localization/03"
run_runner downloader/07
expect_rc 0; expect_ran "downloader/07"

case_start "dir/* selects every harness of a service"
run_runner 'localization/*'
expect_rc 0; expect_ran "localization/03"
run_runner 'downloader/*'
expect_rc 0; expect_ran "downloader/07 downloader/11"

case_start "an unknown selector runs nothing and fails"
run_runner nonsense
expect_rc 1; expect_ran ""

case_start "the summary keeps the downloader's old label and prefixes the others"
run_runner 07 localization/03
expect_out '^phase-07  OK  caught=2/2 controls-green=1/1'
expect_out '^localization/phase-03  OK  caught=3/3 controls-green=0/0'
rm -rf "$SANDBOX"

# --- the guards --------------------------------------------------------------

new_sandbox "$ROWS"; good_harnesses
harness localization 03 "$(caught 2)"
case_start "guard B: fewer outcomes than declared is COUNT"
run_runner localization/03
expect_rc 1; expect_out "localization/phase-03  COUNT"
rm -rf "$SANDBOX"

new_sandbox "$ROWS"; good_harnesses
harness downloader 07 "$(caught 2)"
case_start "guard B: a declared control that never reported is COUNT"
run_runner 07
expect_rc 1; expect_out "phase-07  COUNT"
rm -rf "$SANDBOX"

new_sandbox "$ROWS"; good_harnesses
harness localization 03 "$(caught 2)
echo 'ERROR: mutation matched nothing -- the code shape changed'"
case_start "guard C: matched-nothing is STALE even when the harness exits 0"
run_runner localization/03
expect_rc 1; expect_out "localization/phase-03  STALE"
rm -rf "$SANDBOX"

new_sandbox "$ROWS"; good_harnesses
harness localization 03 "$(caught 2)
echo 'FAIL: suite stayed GREEN with the invariant broken'"
case_start "a mutation that stays green is RED"
run_runner localization/03
expect_rc 1; expect_out "localization/phase-03  RED"
rm -rf "$SANDBOX"

new_sandbox "$ROWS"; good_harnesses
rm "$SANDBOX/services/localization/tests/mutations/phase-03.sh"
case_start "a declared harness that does not exist is MISSING"
run_runner localization/03
expect_rc 1; expect_out "phase-03.sh is declared in EXPECT but does not exist"
rm -rf "$SANDBOX"

new_sandbox $'downloader:07:2:1\nmystery:03:3:0'; good_harnesses
case_start "a row naming an unknown service fails loudly"
run_runner 'mystery/*'
expect_rc 1; expect_out "unknown service 'mystery'"
rm -rf "$SANDBOX"

# --- guard D: a harness that leaves a mutation behind -------------------------

leave_behind() { # LABEL FILE OLD-CONTENT-APPEND
  local label=$1 file=$2 line=$3
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 3)
echo '$line' >> '$SANDBOX/$file'"
  case_start "guard D: a harness that does not restore a $label file is POISONED"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  POISONED"
  rm -rf "$SANDBOX"
}

leave_behind ".py" services/localization/src/mod.py "import os"
leave_behind ".tsx" services/renderer/src/view.tsx "export const leaked = 1"
leave_behind ".json" contracts/schema.json "{}"
leave_behind ".ttf" assets/fonts/face.ttf "changed"
leave_behind ".ts (downloader, as before)" services/downloader/src/thing.ts "export const leaked = 1"

new_sandbox "$ROWS"; good_harnesses
harness localization 03 "$(caught 3)
echo 'x = 2' > '$SANDBOX/services/localization/src/mod.py'
echo 'x = 1' > '$SANDBOX/services/localization/src/mod.py'"
case_start "control: a harness that restores the file byte for byte is not POISONED"
run_runner localization/03
expect_rc 0; expect_out "localization/phase-03  OK"
rm -rf "$SANDBOX"

new_sandbox "$ROWS"; good_harnesses
mkdir -p "$SANDBOX/services/localization/.venv/lib" "$SANDBOX/services/localization/src/pkg/__pycache__"
harness localization 03 "$(caught 3)
echo 'noise' >> '$SANDBOX/services/localization/.venv/lib/site.py'
echo 'noise' >> '$SANDBOX/services/localization/src/pkg/__pycache__/m.py'"
case_start "control: virtualenv and bytecode-cache churn is not source and is ignored"
run_runner localization/03
expect_rc 0; expect_out "localization/phase-03  OK"
rm -rf "$SANDBOX"

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "run-mutations.sh: $CASES cases, all as expected."
  exit 0
fi
echo "run-mutations.sh: $FAILURES expectation(s) failed across $CASES cases."
exit 1

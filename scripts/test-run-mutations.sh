#!/usr/bin/env bash
#
# Tests for scripts/run-mutations.sh itself: selection, and the guards that make a
# hollow or leaky harness a build failure. Runs the REAL runner against throwaway
# harnesses in a sandbox copy of the repo layout, so no real harness is executed.
#
#   bash scripts/test-run-mutations.sh
#
# Two layers:
#
#   1. CASES. Each case builds a sandbox, runs the runner and checks the verdict.
#   2. RUNNER MUTATIONS. A guard is only trusted if removing it turns a case red,
#      so for every guard the test applies a one-line edit that disables it to a
#      copy of the runner, re-runs the matching case against that copy, and
#      requires the case to FAIL. An edit that changes nothing is an error, never
#      a pass (the same rule the harnesses follow).

set -u

REAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0
CASES=0
QUIET=0
RUNNER_SED=""
RUNNER_MUTATION_APPLIED=1

fail() {
  FAILURES=$((FAILURES + 1))
  [ "$QUIET" -eq 1 ] || echo "  FAIL: $*"
}

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
pathlib.Path(os.environ["SANDBOX"], "scripts/run-mutations.sh").write_text(new)
PY
  RUNNER_MUTATION_APPLIED=1
  if [ -n "$RUNNER_SED" ]; then
    cp "$SANDBOX/scripts/run-mutations.sh" "$SANDBOX/runner.orig"
    sed -i.bak -E "$RUNNER_SED" "$SANDBOX/scripts/run-mutations.sh"
    cmp -s "$SANDBOX/runner.orig" "$SANDBOX/scripts/run-mutations.sh" && RUNNER_MUTATION_APPLIED=0
  fi
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

outcome_lines() { # TEXT N -> N copies of an echo of TEXT
  local text=$1 n=$2 i out=""
  for i in $(seq 1 "$n"); do out="${out}echo '$text'; "; done
  printf '%s' "$out"
}
caught() { outcome_lines 'OK: caught. Failing tests:' "$1"; }
green() { outcome_lines 'OK: still green, as required.' "$1"; }

# run_runner ARGS... -> sets RC and OUT
run_runner() {
  : >"$SANDBOX/ran.log"
  OUT=$(bash "$SANDBOX/scripts/run-mutations.sh" "$@" 2>&1)
  RC=$?
}

ran() { # which harness scripts ran, as dir/phase tokens
  sed -E 's#(.*/)?services/([a-z]+)/tests?/mutations/phase-([^.]+)\.sh#\2/\3#' "$SANDBOX/ran.log" \
    | sort | tr '\n' ' ' | sed 's/ $//'
}

case_start() { CASES=$((CASES + 1)); [ "$QUIET" -eq 1 ] || echo "case: $1"; }

expect_rc() {
  [ "$RC" -eq "$1" ] || {
    fail "expected exit $1, got $RC"
    [ "$QUIET" -eq 1 ] || echo "$OUT" | tail -25 | sed 's/^/    | /'
  }
}
expect_ran() { [ "$(ran)" = "$1" ] || fail "expected to run [$1], ran [$(ran)]"; }
expect_out() { grep -qE "$1" <<<"$OUT" || fail "output lacks /$1/"; }

ROWS=$'downloader:07:2:1\ndownloader:11:1:0\nlocalization:03:3:0'

good_harnesses() {
  harness downloader 07 "$(caught 2)$(green 1)"
  harness downloader 11 "$(caught 1)"
  harness localization 03 "$(caught 3)"
}

# --- cases -------------------------------------------------------------------
# Each case function builds its own sandbox, asserts, and removes it.

case_no_selector_runs_all() {
  case_start "no selector runs every harness"
  new_sandbox "$ROWS"; good_harnesses
  run_runner
  expect_rc 0; expect_ran "downloader/07 downloader/11 localization/03"
  rm -rf "$SANDBOX"
}

case_bare_phase_is_downloaders() {
  case_start "a bare phase means the downloader's, and only that one"
  new_sandbox "$ROWS"; good_harnesses
  run_runner 07
  expect_rc 0; expect_ran "downloader/07"
  run_runner 11
  expect_rc 0; expect_ran "downloader/11"
  rm -rf "$SANDBOX"
}

case_bare_03_is_not_localization() {
  case_start "bare 03 does not select localization:03 (it never did)"
  new_sandbox "$ROWS"; good_harnesses
  run_runner 03
  expect_rc 1; expect_ran ""; expect_out "no harness ran"
  rm -rf "$SANDBOX"
}

case_dir_phase_and_dir_star() {
  case_start "dir/phase selects one harness; dir/* selects a whole service"
  new_sandbox "$ROWS"; good_harnesses
  run_runner localization/03
  expect_rc 0; expect_ran "localization/03"
  run_runner downloader/07
  expect_rc 0; expect_ran "downloader/07"
  run_runner 'localization/*'
  expect_rc 0; expect_ran "localization/03"
  run_runner 'downloader/*'
  expect_rc 0; expect_ran "downloader/07 downloader/11"
  rm -rf "$SANDBOX"
}

case_unknown_selector() {
  case_start "an unknown selector runs nothing and fails"
  new_sandbox "$ROWS"; good_harnesses
  run_runner nonsense
  expect_rc 1; expect_ran ""
  rm -rf "$SANDBOX"
}

case_summary_labels() {
  case_start "the summary keeps the downloader's old label and prefixes the others"
  new_sandbox "$ROWS"; good_harnesses
  run_runner 07 localization/03
  expect_out '^phase-07  OK  caught=2/2 controls-green=1/1'
  expect_out '^localization/phase-03  OK  caught=3/3 controls-green=0/0'
  rm -rf "$SANDBOX"
}

case_fewer_mutations() {
  case_start "guard B: fewer mutation outcomes than declared is COUNT"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 2)"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  COUNT"
  rm -rf "$SANDBOX"
}

case_more_mutations() {
  case_start "guard B: MORE mutation outcomes than declared (3 declared, 4 printed) is COUNT"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 4)"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  COUNT"
  rm -rf "$SANDBOX"
}

case_fewer_controls() {
  case_start "guard B: a declared control that never reported is COUNT"
  new_sandbox "$ROWS"; good_harnesses
  harness downloader 07 "$(caught 2)"
  run_runner 07
  expect_rc 1; expect_out "phase-07  COUNT"
  rm -rf "$SANDBOX"
}

case_more_controls() {
  case_start "guard B: MORE controls than declared (1 declared, 2 printed) is COUNT"
  new_sandbox "$ROWS"; good_harnesses
  harness downloader 07 "$(caught 2)$(green 2)"
  run_runner 07
  expect_rc 1; expect_out "phase-07  COUNT"
  rm -rf "$SANDBOX"
}

case_matched_nothing() {
  case_start "guard C: matched-nothing is STALE even when the harness exits 0"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 2)
echo 'ERROR: mutation matched nothing -- the code shape changed'"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  STALE"
  rm -rf "$SANDBOX"
}

case_stays_green() {
  case_start "a mutation that stays green is RED"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 2)
echo 'FAIL: suite stayed GREEN with the invariant broken'"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  RED"
  rm -rf "$SANDBOX"
}

case_control_red() {
  case_start "a control that goes red is RED"
  new_sandbox "$ROWS"; good_harnesses
  harness downloader 07 "$(caught 2)
echo 'FAIL: control allowlisted-imports went red'"
  run_runner 07
  expect_rc 1; expect_out "phase-07  RED"
  rm -rf "$SANDBOX"
}

case_no_tests() {
  case_start "a mutation whose pattern selected no tests is NO-TESTS"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 2)
echo 'ERROR: the test pattern tests/x.py selected no tests.'"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  NO-TESTS"
  rm -rf "$SANDBOX"
}

case_exit_code_alone() {
  case_start "a harness exiting 1 with correct counts and no failure line is EXIT-1"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 3)
exit 1"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  EXIT-1"
  rm -rf "$SANDBOX"
}

case_missing_alongside_good() {
  case_start "a declared harness that does not exist fails the run, and the good one still runs"
  new_sandbox "$ROWS"; good_harnesses
  rm "$SANDBOX/services/localization/tests/mutations/phase-03.sh"
  run_runner
  expect_rc 1
  expect_out "phase-03.sh is declared in EXPECT but does not exist"
  expect_ran "downloader/07 downloader/11"
  expect_out '^phase-07  OK'
  rm -rf "$SANDBOX"
}

case_missing_alone() {
  case_start "a declared harness that does not exist, run alone, fails with the reason"
  new_sandbox "$ROWS"; good_harnesses
  rm "$SANDBOX/services/localization/tests/mutations/phase-03.sh"
  run_runner localization/03
  expect_rc 1; expect_out "phase-03.sh is declared in EXPECT but does not exist"
  rm -rf "$SANDBOX"
}

case_unknown_service() {
  case_start "a row naming an unknown service fails loudly"
  new_sandbox $'downloader:07:2:1\nmystery:03:3:0'; good_harnesses
  run_runner 'mystery/*'
  expect_rc 1; expect_out "unknown service 'mystery'"
  rm -rf "$SANDBOX"
}

# leave_behind FILE-KIND FILE LINE: a harness that mutates FILE and never restores it.
leave_behind() {
  local file=$2 line=$3
  case_start "guard D: a harness that does not restore a $1 file is POISONED"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 3)
echo '$line' >> '$SANDBOX/$file'"
  run_runner localization/03
  expect_rc 1; expect_out "localization/phase-03  POISONED"
  rm -rf "$SANDBOX"
}
case_poison_py() { leave_behind ".py" services/localization/src/mod.py "import os"; }
case_poison_tsx() { leave_behind ".tsx" services/renderer/src/view.tsx "export const leaked = 1"; }
case_poison_json() { leave_behind ".json" contracts/schema.json "{}"; }
case_poison_ttf() { leave_behind ".ttf" assets/fonts/face.ttf "changed"; }
case_poison_ts() { leave_behind ".ts (downloader, as before)" services/downloader/src/thing.ts "export const leaked = 1"; }

case_exact_restore_is_clean() {
  case_start "control: a harness that restores the file byte for byte is not POISONED"
  new_sandbox "$ROWS"; good_harnesses
  harness localization 03 "$(caught 3)
echo 'x = 2' > '$SANDBOX/services/localization/src/mod.py'
echo 'x = 1' > '$SANDBOX/services/localization/src/mod.py'"
  run_runner localization/03
  expect_rc 0; expect_out "localization/phase-03  OK"
  rm -rf "$SANDBOX"
}

case_cache_churn_is_ignored() {
  case_start "control: virtualenv and bytecode-cache churn is not source and is ignored"
  new_sandbox "$ROWS"; good_harnesses
  mkdir -p "$SANDBOX/services/localization/.venv/lib" "$SANDBOX/services/localization/src/pkg/__pycache__"
  harness localization 03 "$(caught 3)
echo 'noise' >> '$SANDBOX/services/localization/.venv/lib/site.py'
echo 'noise' >> '$SANDBOX/services/localization/src/pkg/__pycache__/m.py'"
  run_runner localization/03
  expect_rc 0; expect_out "localization/phase-03  OK"
  rm -rf "$SANDBOX"
}

ALL_CASES="
case_no_selector_runs_all case_bare_phase_is_downloaders case_bare_03_is_not_localization
case_dir_phase_and_dir_star case_unknown_selector case_summary_labels
case_fewer_mutations case_more_mutations case_fewer_controls case_more_controls
case_matched_nothing case_stays_green case_control_red case_no_tests case_exit_code_alone
case_missing_alongside_good case_missing_alone case_unknown_service
case_poison_py case_poison_tsx case_poison_json case_poison_ttf case_poison_ts
case_exact_restore_is_clean case_cache_churn_is_ignored
"

echo "== cases =="
for c in $ALL_CASES; do "$c"; done

# --- runner mutations ----------------------------------------------------------
# NAME | sed expression that disables ONE check of the runner | case that must notice
RUNNER_MUTATIONS=(
  'guard B ignores extra mutation outcomes|s/\[ "\$outcomes" -ne "\$expect_mutations" \]/[ "$outcomes" -lt "$expect_mutations" ]/|case_more_mutations'
  'guard B ignores missing mutation outcomes|s/\[ "\$outcomes" -ne "\$expect_mutations" \]/[ "$outcomes" -gt "$expect_mutations" ]/|case_fewer_mutations'
  'guard B ignores extra control outcomes|s/\[ "\$control_outcomes" -ne "\$expect_controls" \]/[ "$control_outcomes" -lt "$expect_controls" ]/|case_more_controls'
  'guard B ignores missing control outcomes|s/\[ "\$control_outcomes" -ne "\$expect_controls" \]/[ "$control_outcomes" -gt "$expect_controls" ]/|case_fewer_controls'
  'guard C ignores matched-nothing|s/if \[ "\$empty" -gt 0 \]/if false/|case_matched_nothing'
  'no-tests outcome is ignored|s/if \[ "\$no_tests" -gt 0 \]/if false/|case_no_tests'
  'red outcomes are ignored|s/if \[ "\$stayed_green" -gt 0 \] \|\| \[ "\$control_red" -gt 0 \]/if false/|case_stays_green'
  'red controls are ignored|s/if \[ "\$stayed_green" -gt 0 \] \|\| \[ "\$control_red" -gt 0 \]/if false/|case_control_red'
  'guard D is off|s/\[ "\$before" != "\$after" \]/false/|case_poison_py'
  'a nonzero exit with good counts passes|s/\[ "\$rc" -ne 0 \] \&\& \[ "\$status" = "OK" \]/false/|case_exit_code_alone'
  'a missing harness is not checked|s/\[ ! -f "\$script" \]/false/|case_missing_alongside_good'
  'an unknown service is not checked|s/if ! hdir=\$\(harness_dir "\$dir"\)/if false/|case_unknown_service'
  'a bare phase also selects other services|s/\[ "\$dir" = downloader \] \&\& \[ "\$want" = "\$phase" \]/[ "$want" = "$phase" ]/|case_bare_03_is_not_localization'
  'dir/* selects nothing|s/\[ "\$want" = "\$dir\/\*" \] \&\& return 0//|case_dir_phase_and_dir_star'
  'fingerprint skips .py|s/-name .\*\.py. -o //|case_poison_py'
  'fingerprint skips .tsx|s/-name .\*\.tsx. -o //|case_poison_tsx'
  'fingerprint skips .json|s/-name .\*\.json. -o //|case_poison_json'
  'fingerprint skips .ttf|s/-name .\*\.ttf. -o //|case_poison_ttf'
  'fingerprint skips .ts|s/-name .\*\.ts. -o //|case_poison_ts'
  'fingerprint does not prune caches|s/-prune -o/-o/|case_cache_churn_is_ignored'
)

echo
echo "== runner mutations: every guard must be load-bearing =="
for entry in "${RUNNER_MUTATIONS[@]}"; do
  name=${entry%%|*}
  rest=${entry#*|}
  expr=${rest%|*}
  fn=${rest##*|}
  before=$FAILURES
  RUNNER_SED=$expr
  QUIET=1
  "$fn"
  QUIET=0
  RUNNER_SED=""
  if [ "$RUNNER_MUTATION_APPLIED" -eq 0 ]; then
    echo "  ERROR: runner mutation '$name' changed nothing; update its sed."
    FAILURES=$((before + 1))
  elif [ "$FAILURES" -eq "$before" ]; then
    echo "  FAIL: with the runner mutated ('$name'), $fn stayed green: that guard is not tested."
    FAILURES=$((before + 1))
  else
    echo "  OK: caught -- $name ($fn)"
    FAILURES=$before
  fi
done

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "run-mutations.sh: $CASES cases and ${#RUNNER_MUTATIONS[@]} runner mutations, all as expected."
  exit 0
fi
echo "run-mutations.sh: $FAILURES expectation(s) failed."
exit 1

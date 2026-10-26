#!/usr/bin/env bash
#
# The mutation-harness protocol, for harnesses that are not jest.
#
# scripts/run-mutations.sh does not read a harness's exit code to decide what
# happened; it counts the OUTCOME LINES a harness prints (see the grep block in
# that file) and compares them to the manifest. The functions here print exactly
# those lines, so a harness written against this library cannot drift from what
# the runner counts. They also enforce, once, the rules every harness has had to
# re-learn (plan.md section 5):
#
#   1. A mutation that changes nothing is a hard error, never a pass.
#   2. "Caught" needs positive evidence: the tests RAN (a pytest summary line),
#      the run FINISHED (bounded by a timeout), and the failing tests include the
#      ones that exist to catch this mutation. A failure elsewhere does not count.
#   3. Every mutated file is restored from a pristine snapshot on ANY exit, and a
#      restore that did not restore is fatal.
#   4. A baseline run must be green before the first mutation.
#
# Outcome lines (one per mutation / control; the runner counts these):
#   OK: caught. Failing tests:             mutation, went red in the right test
#   FAIL: suite stayed GREEN ...           mutation, stayed green
#   ERROR: mutation matched nothing ...    mutation did not apply
#   ERROR: the test pattern ...            mutation, red for an unrelated reason,
#                                          or no tests selected
#   OK: still green, as required.          control, stayed green
#   FAIL: control ...                      control went red / selected no tests
#   NOTE: restored from the pristine snapshot   (on_exit had to restore)
#
# Usage:
#
#   source scripts/lib/mutation-protocol.sh
#   mp_init file1 file2 ...                       # snapshot, install the exit trap
#   MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)
#   mp_baseline "tests/a.py tests/b.py"
#
#   mp_mutation "what this breaks"
#   apply_mutation file 'old text' 'new text' [file 'old' 'new' ...] \
#     && expect_red "tests/a.py" "test_name_that_guards_it" ["another"]
#   restore
#
#   mp_mutation "a change that must not matter"
#   apply_mutation file 'old' 'new' && expect_green "tests/a.py" "label"
#   restore
#
#   mp_finish                                     # exit 1 if anything failed

MP_FAIL=0
MP_FILES=()
MP_SNAP=""
MP_TIMEOUT_SECONDS=${MP_TIMEOUT_SECONDS:-180}

# --- snapshot / restore -----------------------------------------------------

mp_init() {
  MP_FILES=("$@")
  MP_SNAP=$(mktemp -d)
  local i=0 f
  for f in "${MP_FILES[@]}"; do
    cp "$f" "$MP_SNAP/$i"
    i=$((i + 1))
  done
  trap mp_on_exit EXIT
}

# 0 when every file is byte-identical to its snapshot.
mp_pristine() {
  local i=0 f
  for f in "${MP_FILES[@]}"; do
    cmp -s "$MP_SNAP/$i" "$f" || return 1
    i=$((i + 1))
  done
  return 0
}

mp_copy_back() {
  local i=0 f
  for f in "${MP_FILES[@]}"; do
    cp "$MP_SNAP/$i" "$f"
    i=$((i + 1))
  done
}

restore() {
  mp_copy_back
  if ! mp_pristine; then
    echo "FATAL: failed to restore the mutated files"
    exit 1
  fi
}

mp_on_exit() {
  if [ -n "$MP_SNAP" ] && ! mp_pristine; then
    echo "NOTE: restored from the pristine snapshot"
    mp_copy_back
  fi
  [ -n "$MP_SNAP" ] && rm -rf "$MP_SNAP"
}

# --- applying a mutation ----------------------------------------------------

mp_mutation() {
  echo
  echo "=============================================================="
  echo "MUTATION: $1"
  echo "=============================================================="
}

# apply_mutation FILE OLD NEW [FILE OLD NEW ...]
#
# Replaces the one exact occurrence of OLD with NEW in FILE. OLD of '@append'
# appends NEW. Every triple must match exactly once, otherwise nothing is kept,
# the matched-nothing line is printed and the function returns 1.
apply_mutation() {
  local before
  before=$(mktemp -d)
  local files=() f
  while [ "$#" -ge 3 ]; do
    f=$1
    files+=("$f")
    cp "$f" "$before/$(( ${#files[@]} - 1 ))"
    if ! MP_FILE="$f" MP_OLD="$2" MP_NEW="$3" python3 - <<'PY'
import os, pathlib, sys
path = pathlib.Path(os.environ["MP_FILE"])
old, new = os.environ["MP_OLD"], os.environ["MP_NEW"]
text = path.read_text()
if old == "@append":
    path.write_text(text + new)
elif text.count(old) == 1:
    path.write_text(text.replace(old, new))
else:
    sys.exit(1)
PY
    then
      # Put back whatever earlier triples of this call already changed.
      local i=0
      for f in "${files[@]}"; do cp "$before/$i" "$f"; i=$((i + 1)); done
      rm -rf "$before"
      echo "ERROR: mutation matched nothing -- the code shape changed and this"
      echo "       mutation no longer applies. Update it; do NOT treat this as a pass."
      MP_FAIL=1
      return 1
    fi
    shift 3
  done
  # Paranoia, the way the other harnesses do it: the diff must be non-empty.
  local i=0 changed=0
  for f in "${files[@]}"; do
    cmp -s "$before/$i" "$f" || changed=1
    i=$((i + 1))
  done
  if [ "$changed" -eq 0 ]; then
    rm -rf "$before"
    echo "ERROR: mutation matched nothing -- applying it changed no byte."
    MP_FAIL=1
    return 1
  fi
  echo "--- diff actually applied ---"
  i=0
  for f in "${files[@]}"; do
    diff "$before/$i" "$f"
    i=$((i + 1))
  done
  echo "-----------------------------"
  rm -rf "$before"
  return 0
}

# --- running the tests ------------------------------------------------------

mp_kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do mp_kill_tree "$child"; done
  kill -9 "$pid" 2>/dev/null
}

# mp_run_tests "<pytest args>" -> exit: 0 green, 1 red, 98 timeout, 99 nothing ran.
# The output goes to $MP_SNAP/out.log.
mp_run_tests() {
  local args=$1
  # shellcheck disable=SC2086
  "${MP_TEST_CMD[@]}" -rfE $args >"$MP_SNAP/out.log" 2>&1 &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$MP_TIMEOUT_SECONDS" ]; then
      mp_kill_tree "$pid"
      wait "$pid" 2>/dev/null
      return 98
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$pid"
  local rc=$?
  # Positive evidence that tests ran: pytest prints "N passed" / "N failed".
  if ! grep -qE '[0-9]+ (passed|failed)' "$MP_SNAP/out.log"; then
    # Exit 0 or 5 without a count is "selected nothing"; a crash is "red" but
    # with no test evidence, which the caller treats as unrelated.
    [ "$rc" -eq 0 ] || [ "$rc" -eq 5 ] && return 99
  fi
  [ "$rc" -eq 0 ] && return 0
  return 1
}

# mp_baseline "<pytest args>": the suite must be green, with tests, before the
# first mutation. Otherwise every "caught" below would be a lie.
mp_baseline() {
  mp_run_tests "$1"
  case $? in
    0) echo "BASELINE: green" ;;
    *)
      echo "FATAL: the baseline is not green (or ran nothing); fix it before mutating."
      tail -30 "$MP_SNAP/out.log"
      exit 1
      ;;
  esac
}

# expect_red "<pytest args>" TEST_NAME...
expect_red() {
  local args=$1
  shift
  echo "  tests: $args (must FAIL)"
  mp_run_tests "$args"
  local rc=$?
  case $rc in
    1)
      local missing=0 t
      for t in "$@"; do
        if ! grep -qE "^FAILED [^ ]*$t" "$MP_SNAP/out.log"; then
          echo "FAIL: suite went red, but not in the test that guards this: $t"
          missing=1
        fi
      done
      if [ "$missing" -eq 0 ]; then
        echo "OK: caught. Failing tests:"
        grep -E '^FAILED ' "$MP_SNAP/out.log"
      else
        echo "ERROR: the test pattern $args went red for an unrelated reason."
        grep -E '^(FAILED|ERROR) ' "$MP_SNAP/out.log" || tail -10 "$MP_SNAP/out.log"
        MP_FAIL=1
      fi
      ;;
    0)
      echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
      tail -20 "$MP_SNAP/out.log"
      MP_FAIL=1
      ;;
    98)
      echo "FAIL: the run timed out after ${MP_TIMEOUT_SECONDS}s."
      MP_FAIL=1
      ;;
    *)
      echo "ERROR: the test pattern $args selected no tests."
      tail -10 "$MP_SNAP/out.log"
      MP_FAIL=1
      ;;
  esac
}

# expect_green "<pytest args>" LABEL
expect_green() {
  local args=$1 label=$2
  echo "  tests: $args (control, must stay GREEN)"
  mp_run_tests "$args"
  case $? in
    0) echo "OK: still green, as required." ;;
    99)
      echo "FAIL: control $label selected no tests."
      tail -10 "$MP_SNAP/out.log"
      MP_FAIL=1
      ;;
    98)
      echo "FAIL: control $label timed out after ${MP_TIMEOUT_SECONDS}s."
      MP_FAIL=1
      ;;
    *)
      echo "FAIL: control $label went red -- the mechanism is not what carries the invariant."
      grep -E '^(FAILED|ERROR) ' "$MP_SNAP/out.log" || tail -20 "$MP_SNAP/out.log"
      MP_FAIL=1
      ;;
  esac
}

mp_finish() {
  if ! mp_pristine; then
    echo "FATAL: the mutated files were not restored"
    exit 1
  fi
  exit "$MP_FAIL"
}

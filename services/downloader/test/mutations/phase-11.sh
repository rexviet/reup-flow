#!/usr/bin/env bash
#
# Mutation suite for S3Storage (src/storage/s3.storage.ts).
#
# A suite that is green is evidence it RAN, not that the invariant holds. Each
# mutation below breaks one invariant on purpose and REQUIRES the S3
# integration suites to go red. The rules every harness in this repo follows,
# each there because a harness here has already lied in that specific way:
#
#   1. Every mutation prints the diff it actually applied. An empty diff is a
#      hard error, never a pass.
#   2. A mutation is evidence only if tests actually ran: "Tests: N ..." with
#      N > 0. Exit code alone cannot tell "caught" from "selected nothing".
#   3. The restore is verified, and runs from a pristine snapshot on ANY exit
#      (trap on EXIT INT TERM), saying so when it fires.
#   4. A mutation can HANG instead of failing; a timeout is its own outcome,
#      and the whole process tree is killed, not just the pnpm wrapper.
#
# Needs the compose stack up AND a reachable S3 endpoint. MinIO publishes no S3
# port, so point S3_ENDPOINT at a loopback proxy onto the compose network, and
# export S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY (the downloader's MinIO user).
#
# Run from services/downloader:  bash test/mutations/phase-11.sh

set -u

cd "$(dirname "$0")/../.." || exit 1

STORAGE=src/storage/s3.storage.ts
PATTERN='s3\.'
JEST_TIMEOUT_SECONDS=240

for var in S3_ENDPOINT S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY; do
  if [ -z "${!var:-}" ]; then
    echo "FATAL: $var is not set; the S3 suites cannot run, so no mutation can be measured."
    exit 2
  fi
done

SNAP=$(mktemp -d)
cp "$STORAGE" "$SNAP/s3.storage.ts.pristine"
fail=0
EXPECT_MUTATIONS=5
caught=0

restore() { cp "$SNAP/s3.storage.ts.pristine" "$STORAGE"; }
verify_restored() {
  if ! diff -q "$SNAP/s3.storage.ts.pristine" "$STORAGE" >/dev/null; then
    echo "FATAL: failed to restore the pristine $STORAGE"
    exit 1
  fi
}
on_exit() {
  if ! diff -q "$SNAP/s3.storage.ts.pristine" "$STORAGE" >/dev/null 2>&1; then
    echo "NOTE: restored from the pristine snapshot"
    restore
  fi
  rm -rf "$SNAP"
}
trap on_exit EXIT
trap 'exit 130' INT TERM

kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill "$pid" 2>/dev/null
}

# run_jest: 0 green, 1 red, 98 timeout, 99 nothing ran.
run_jest() {
  pnpm exec jest -c jest.integration.config.js "$PATTERN" >"$SNAP/out.log" 2>&1 &
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

# Edits go through python so a no-match is an empty diff, which apply_mutation rejects.
edit() { # edit <file> <old> <new>
  python3 - "$@" <<'PY'
import pathlib, sys
f, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(f); s = p.read_text()
p.write_text(s.replace(old, new, 1))
PY
}

run_mutation() {
  local label=$1
  shift
  echo
  echo "=============================================================="
  echo "MUST-FAIL: $label"
  echo "=============================================================="
  "$@"
  if diff -q "$SNAP/s3.storage.ts.pristine" "$STORAGE" >/dev/null; then
    echo "ERROR: mutation matched nothing -- the code shape changed and this"
    echo "       mutation no longer applies. Update it; do NOT treat this as a pass."
    fail=1
    restore; verify_restored
    return
  fi
  echo "--- diff actually applied ---"
  diff "$SNAP/s3.storage.ts.pristine" "$STORAGE"
  echo "-----------------------------"
  run_jest; local rc=$?
  case $rc in
    99) echo "ERROR: the test pattern '$PATTERN' selected no tests"; fail=1 ;;
    98) echo "OK: caught, but by HANGING -- ${JEST_TIMEOUT_SECONDS}s timeout"; caught=$((caught + 1)) ;;
    0)  echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
        tail -20 "$SNAP/out.log"; fail=1 ;;
    *)  echo "OK: caught. Failing tests:"; grep -E '✕|●.*›' "$SNAP/out.log" | sort -u | head -12 \
          || tail -10 "$SNAP/out.log"
        caught=$((caught + 1)) ;;
  esac
  restore; verify_restored
}

# A declined commit leaves the upload open: a full-size video per reaped download.
run_mutation "decline no longer aborts the multipart upload" \
  edit "$STORAGE" "      if (beforeCommit && !(await beforeCommit())) {
        await this.abort(objectKey, uploadId);
        return null;" "      if (beforeCommit && !(await beforeCommit())) {
        return null;"

# A failed put leaves its parts in the store, billed and invisible to exists().
run_mutation "a failed put no longer aborts its upload" \
  edit "$STORAGE" "    } catch (err) {
      await this.abort(objectKey, uploadId);
      throw err;" "    } catch (err) {
      throw err;"

# A truncated body commits as if it were complete.
run_mutation "the advertised byte count is no longer enforced" \
  edit "$STORAGE" "if (expectedBytes !== undefined && bytes !== expectedBytes) {" \
  "if (expectedBytes !== undefined && bytes < 0) {"

# The sweep aborts uploads of the same process that are still being written.
run_mutation "the sweep ignores the in-flight registry" \
  edit "$STORAGE" "        if (this.inFlight.has(uploadId)) continue;
" ""

# The sweep aborts another process's healthy upload: a lease-sized cutoff.
run_mutation "the sweep uses the lease instead of the stale-upload threshold" \
  edit "$STORAGE" "const threshold = Math.max(olderThanMs, this.opts.staleUploadMs);" \
  "const threshold = olderThanMs;"

echo
if [ "$caught" -ne "$EXPECT_MUTATIONS" ]; then
  echo "FAIL: caught $caught/$EXPECT_MUTATIONS mutations -- the harness did not measure everything it declares."
  fail=1
fi
if [ "$fail" -ne 0 ]; then
  echo "phase-11: FAILED"
  exit 1
fi
echo "phase-11: every mutation caught"

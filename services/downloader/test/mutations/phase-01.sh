#!/usr/bin/env bash
#
# Phase 1 mutation suite (plan.md §5, phase-01 DoD).
#
# "The suite is green" is evidence the suite ran, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# corresponding tests to go red. Two rules, both learned the hard way on this
# repo:
#
#   1. Every mutation prints the diff it actually applied. A sed that matches
#      nothing is a mutation that never ran -- reporting that as "passed" is
#      how a guard silently dies. Any mutation whose diff is empty is a hard
#      error here, not a skip.
#   2. Every mutation restores its file, and the restore is verified.
#
# Run from services/downloader:  bash test/mutations/phase-01.sh
# Requires a Postgres reachable at TEST_DATABASE_URL (docker compose up).

set -u

cd "$(dirname "$0")/../.." || exit 1

VIDEOS_REPO=src/db/videos.repository.ts
JOBS_REPO=src/db/jobs.repository.ts
MIGRATION=migrations/260824-init-schema.sql
SCHEMA=src/db/schema.ts

BACKUP_DIR=$(mktemp -d)
trap 'rm -rf "$BACKUP_DIR"' EXIT
fail=0

backup() { cp "$1" "$BACKUP_DIR/$(basename "$1").bak"; }
restore() {
  cp "$BACKUP_DIR/$(basename "$1").bak" "$1"
  diff -q "$BACKUP_DIR/$(basename "$1").bak" "$1" >/dev/null \
    || { echo "FATAL: failed to restore $1"; exit 1; }
}

# run_mutation <label> <file> <test-pattern> <mutator-command...>
#
# Applies the mutation, refuses to continue if it changed nothing, requires
# the named tests to fail, then restores.
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

  if pnpm test -- "$pattern" >"$BACKUP_DIR/out.log" 2>&1; then
    echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
    tail -20 "$BACKUP_DIR/out.log"
    fail=1
  else
    echo "OK: caught. Failing tests:"
    grep -E '✕' "$BACKUP_DIR/out.log" || tail -10 "$BACKUP_DIR/out.log"
  fi

  restore "$file"
}

# --- plan.md §5: "claim must not double-issue" -------------------------------
# Phase 0 proved this specific mutation can pass a badly-written concurrency
# test: Postgres's LockRows node retries the next candidate row when a
# blocked lock is released, so fast-committing claimers converge on the right
# answer even without SKIP LOCKED. The suite only catches it because winning
# claims are held open until every contender settles.
run_mutation "drop SKIP LOCKED from the video claim" "$VIDEOS_REPO" test/db/videos.repository \
  perl -0pi -e "s/\.for\('update', \{ skipLocked: true \}\)/.for('update')/" "$VIDEOS_REPO"

run_mutation "drop SKIP LOCKED from the job claim" "$JOBS_REPO" test/db/jobs.repository \
  perl -0pi -e "s/\.for\('update', \{ skipLocked: true \}\)/.for('update')/" "$JOBS_REPO"

# --- plan.md §5 / F6: "attempts increments at claim time" ---------------------
# Moving the increment to failure time means a worker killed mid-download --
# which reports nothing at all -- never burns an attempt, so the video is
# re-claimed forever.
run_mutation "increment attempts at failure time instead of claim time (F6)" \
  "$VIDEOS_REPO" test/db/videos.repository \
  python3 -c '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
s = s.replace("      attempts: sql`${videos.attempts} + 1`,\n", "")
s = s.replace("      lastError: error,\n", "      lastError: error,\n      attempts: sql`${videos.attempts} + 1`,\n")
p.write_text(s)
'

# --- phase-01 Risks: "attempts < max_attempts must filter the claim" ---------
# Without it, a video that has exhausted its retries is claimed forever.
run_mutation "stop filtering exhausted videos out of the claim" \
  "$VIDEOS_REPO" test/db/videos.repository \
  perl -0pi -e "s/and\(eq\(videos\.status, 'pending'\), lt\(videos\.attempts, opts\.maxAttempts\)\)/eq(videos.status, 'pending')/" "$VIDEOS_REPO"

# --- phase-01 Risks: partial indexes ----------------------------------------
# A missing partial index changes no answer, only the plan: correctness tests
# stay green while every claim seq-scans the archive.
run_mutation "drop the partial pending-claim index" "$MIGRATION" test/db/claim-plan \
  perl -0pi -e "s/CREATE INDEX videos_pending_claim_idx\n  ON videos \(status, create_time\) WHERE status = 'pending';\n//" "$MIGRATION"

run_mutation "make the pending-claim index non-partial" "$MIGRATION" test/db/schema-migration \
  perl -0pi -e "s/  ON videos \(status, create_time\) WHERE status = 'pending';/  ON videos (status, create_time);/" "$MIGRATION"

# --- phase-01 §1.2: index SHAPE, not just index name ------------------------
# A swapped column order keeps the name, keeps the partial predicate, and is
# still chosen by the planner -- so both the drift test and the plan test
# stay green while the claim can no longer be served in order.
run_mutation "swap the claim index column order" "$MIGRATION" test/db/schema-migration \
  perl -0pi -e "s/  ON videos \(status, create_time\) WHERE status = 'pending';/  ON videos (create_time, status) WHERE status = 'pending';/" "$MIGRATION"

# --- F7: videos must never gain a job_id ------------------------------------
run_mutation "add a job_id column to videos (F7)" "$SCHEMA" test/db/schema-migration \
  perl -0pi -e "s/    lastError: text\('last_error'\),/    lastError: text('last_error'),\n    jobId: text('job_id'),/" "$SCHEMA"

echo
if [ "$fail" -eq 0 ]; then
  echo "All phase-1 mutations applied and were caught."
else
  echo "PHASE 1 MUTATION SUITE FAILED -- see above."
fi
exit "$fail"

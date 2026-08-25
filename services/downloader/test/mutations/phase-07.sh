#!/usr/bin/env bash
#
# Phase 7 mutation suite (plan.md §5, phase-07 DoD).
#
# "The suite is green" is evidence the suite RAN, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# named tests to go red. All NINE enforcement rules from plan §5 are carried
# over, because each exists because of a specific way a mutation harness has
# already lied on this repo:
#
#   1. Every mutation prints the diff it actually applied. Empty diff = hard
#      error, never a pass. (Phase 1, twice.)
#   2. A mutation is evidence only if the code changed AND tests actually ran.
#      "No tests found" is a hard error in both directions. (Phase 3.)
#   3. Every listed file must change, and the restore is verified with one
#      mechanism that works on untracked files. (Phase 3.)
#   4. When an invariant is held by MORE THAN ONE mechanism, a red mutation
#      proves only that the line matters somehow -- not which mechanism carries
#      it. Such invariants also get a CONTROL that must stay GREEN. (Phase 2.)
#   5. A harness that is KILLED must not leave the tree poisoned: pristine
#      snapshot + `trap ... EXIT INT TERM`, and it SAYS SO when it restores.
#      (Phase 4.)
#   6. A mutation can HANG instead of failing. A timeout is its own outcome:
#      a legitimate catch for a must-fail, a FAILURE for a control. (Phase 4.)
#   7. A fixture easier than reality is its own kind of false green, and a
#      mutation that STAYS GREEN is the only channel that catches a comment
#      which is lying. (Phase 5.)
#   8. The "did tests run?" guard must demand POSITIVE evidence -- jest exits 0
#      printing "7 skipped, 7 total" when `-t` matches nothing. And a hang must
#      kill the whole PROCESS TREE, not just the `pnpm` wrapper. (Phase 6.)
#   9. A test double that cannot fail the way the real thing fails makes a green
#      suite silent about that case. For F4 this is the sharpest rule in the
#      list: the probe has FIVE distinguishable outcomes and the fake parser in
#      test/parser/f4-probe.spec.ts can produce every one of them. (Phase 9.)
#
# THE TWO HEADLINE DoD MUTATIONS ARE BLOCK 1. Everything after is a guard this
# phase added on its own, and BLOCK 5 is the one worth reading: the cancelled-
# job revival had NO GUARD AT ALL before phase 7, and its absence was invisible
# because `skipped` counts as settled -- so the job reported `completed` with
# zero downloads and every number agreed. Same shape as phase 6's zero-byte
# commit: the worst bugs here are the ones with no invariant watching them.
#
# Run from services/downloader:  bash test/mutations/phase-07.sh
# Needs the docker-compose stack up (Postgres).

set -u

cd "$(dirname "$0")/../.." || exit 1

PROBE=src/parser/f4-probe.ts
PROBE_HEALTH=src/parser/probe-health.ts
LANE=src/lanes/enumerate.lane.ts
JOBS_REPO=src/db/jobs.repository.ts
VIDEOS_REPO=src/db/videos.repository.ts
JOBS_SERVICE=src/jobs/jobs.service.ts
LANES_MODULE=src/lanes/lanes.module.ts
PARSER_CLIENT=src/parser/parser.client.ts

BACKUP_DIR=$(mktemp -d)
fail=0

# --- RULE 5 -------------------------------------------------------------------
PRISTINE_DIR=$(mktemp -d)
ALL_TARGETS="$PROBE $PROBE_HEALTH $LANE $JOBS_REPO $VIDEOS_REPO $JOBS_SERVICE $LANES_MODULE $PARSER_CLIENT"
for f in $ALL_TARGETS; do cp "$f" "$PRISTINE_DIR/$(basename "$f").pristine"; done

restore_all_from_pristine() {
  local dirty=""
  for f in $ALL_TARGETS; do
    if ! diff -q "$PRISTINE_DIR/$(basename "$f").pristine" "$f" >/dev/null 2>&1; then
      cp "$PRISTINE_DIR/$(basename "$f").pristine" "$f"
      dirty="$dirty $f"
    fi
  done
  if [ -n "$dirty" ]; then
    echo
    echo "NOTE: restored from the pristine snapshot on exit:$dirty"
    echo "      (If this fires on a normal run, a mutation's own restore is broken.)"
  fi
  rm -rf "$BACKUP_DIR" "$PRISTINE_DIR"
}
# EXIT restores. INT/TERM restore AND STOP, which phases 4-6 do not do: their
# handler returns and bash carries straight on to the next mutation, so a `kill`
# leaves the harness running against a tree it has just restored -- and the next
# `_apply` re-poisons it. Restoring is the safety property rule 5 asks for;
# stopping is what makes the kill mean what the person typing it meant.
trap restore_all_from_pristine EXIT
trap 'restore_all_from_pristine; echo "INTERRUPTED -- tree restored, stopping."; exit 130' INT TERM

if ! pnpm lint >/dev/null 2>&1; then
  echo "REFUSING TO RUN: \`pnpm lint\` is already failing before any mutation."
  echo "Fix the tree first; results measured against a broken build mean nothing."
  exit 1
fi

backup() { for f in $1; do cp "$f" "$BACKUP_DIR/$(basename "$f").bak"; done; }
restore() {
  for f in $1; do
    cp "$BACKUP_DIR/$(basename "$f").bak" "$f"
    diff -q "$BACKUP_DIR/$(basename "$f").bak" "$f" >/dev/null \
      || { echo "FATAL: failed to restore $f"; exit 1; }
  done
}

_apply() {
  local label=$1 files=$2
  shift 2
  echo
  echo "=============================================================="
  echo "MUTATION: $label"
  echo "  files: $files"
  echo "=============================================================="
  backup "$files"
  "$@"
  local unchanged=""
  for f in $files; do
    diff -q "$BACKUP_DIR/$(basename "$f").bak" "$f" >/dev/null && unchanged="$unchanged $f"
  done
  if [ -n "$unchanged" ]; then
    echo "ERROR: mutation matched nothing in:$unchanged"
    echo "       The code shape changed and this mutation no longer applies in full."
    echo "       Update it; do NOT treat this as a pass."
    fail=1
    restore "$files"
    return 1
  fi
  echo "--- diff actually applied ---"
  for f in $files; do diff "$BACKUP_DIR/$(basename "$f").bak" "$f"; done
  echo "-----------------------------"
  return 0
}

# `download-concurrency.spec.ts` is the slowest thing any pattern here selects.
# Its own barrier ceiling is 1.5s per unmet batch, so a mutation that serialises
# downloads takes ~18 x 1.5s -- comfortably inside this, and comfortably outside
# anything that is genuinely wedged.
JEST_TIMEOUT_SECONDS=300

# Kills a process and everything under it (RULE 8). `$!` is the pnpm WRAPPER, so
# killing it alone orphans the node process and jest's workers -- which keep
# their Postgres connections and their schemas, so rule 5's "a killed harness
# leaves no poison" would hold for the source tree but not for the DATABASE the
# next mutation is measured against.
kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill -9 "$pid" 2>/dev/null
}

# 0 = green, 1 = red, 98 = never finished, 99 = the pattern selected nothing.
#
# `pnpm exec jest`, NOT `pnpm test -- ...` (RULE 2): everything after `--`
# reaches jest as a POSITIONAL argument, so a control needing `-t Name` gets
# `-t` and `Name` folded into the path regex, jest runs the WHOLE file, and the
# control reports red because the must-fail test beside it is failing exactly as
# intended.
run_jest() {
  # shellcheck disable=SC2086 -- word-splitting is the point (rule 2).
  pnpm exec jest $1 >"$BACKUP_DIR/out.log" 2>&1 &
  local jest_pid=$!
  local waited=0
  while kill -0 "$jest_pid" 2>/dev/null; do
    if [ "$waited" -ge "$JEST_TIMEOUT_SECONDS" ]; then
      kill_tree "$jest_pid"
      wait "$jest_pid" 2>/dev/null
      return 98
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$jest_pid"
  local rc=$?
  if grep -q "No tests found" "$BACKUP_DIR/out.log"; then return 99; fi
  # RULE 8: POSITIVE evidence that at least one test ran. Looking for "0 total"
  # is blind to the case that actually happens -- a `-t` matching nothing prints
  # "Tests: 7 skipped, 7 total" and exits 0, so every control would report
  # "still green, as required" having executed nothing.
  if ! grep -qE "^Tests:.*[0-9]+ (passed|failed)" "$BACKUP_DIR/out.log"; then return 99; fi
  return "$rc"
}

report_no_tests() {
  echo "ERROR: the test pattern '$1' selected NO tests, so this mutation ran"
  echo "       against nothing. That is not a pass and not a failure -- it is a"
  echo "       broken mutation. Fix the pattern."
  fail=1
}

run_mutation() {
  local label=$1 files=$2 pattern=$3
  shift 3
  _apply "$label" "$files" "$@" || return
  echo "  tests: $pattern (must FAIL)"
  run_jest "$pattern"
  local rc=$?
  if [ "$rc" -eq 99 ]; then
    report_no_tests "$pattern"
  elif [ "$rc" -eq 98 ]; then
    echo "OK: caught, but by HANGING rather than failing (${JEST_TIMEOUT_SECONDS}s timeout)."
    echo "    A red test names the defect; a hang does not. Prefer a test that"
    echo "    can report this failure."
  elif [ "$rc" -eq 0 ]; then
    echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
    tail -25 "$BACKUP_DIR/out.log"
    fail=1
  else
    echo "OK: caught. Failing tests:"
    grep -E '✕' "$BACKUP_DIR/out.log" || tail -12 "$BACKUP_DIR/out.log"
  fi
  restore "$files"
}

run_control() {
  local label=$1 files=$2 pattern=$3
  shift 3
  _apply "CONTROL: $label" "$files" "$@" || return
  echo "  tests: $pattern (must STAY GREEN)"
  run_jest "$pattern"
  local rc=$?
  if [ "$rc" -eq 99 ]; then
    report_no_tests "$pattern"
  elif [ "$rc" -eq 98 ]; then
    echo "FAIL: control never finished (${JEST_TIMEOUT_SECONDS}s timeout). A control that"
    echo "      hangs is not green; it is unmeasured."
    fail=1
  elif [ "$rc" -eq 0 ]; then
    echo "OK: still green, as required."
  else
    echo "FAIL: control went red -- the invariant is resting on the wrong mechanism."
    grep -E '✕' "$BACKUP_DIR/out.log" || tail -25 "$BACKUP_DIR/out.log"
    fail=1
  fi
  restore "$files"
}

py() { python3 -c "$1"; }

# ############################################################################
# BLOCK 1 -- THE TWO DoD LINES.
#
#   "Fake parser 400 + probe OK   -> `failed` with a clear reason, NOT `blocked`"
#   "Fake parser 400 + probe fail -> `blocked`"
#
# Both are one bit -- `ProbeResult.global` -- read in one place. So there are two
# mutations, one per end, because either edit alone ships the bug:
#   1a  the probe stops distinguishing (a healthy probe reads as global)
#   1b  the lane stops acting on the distinction (everything parks at `blocked`)
#
# 1b is IDEA.md §6.2 implemented literally -- "3 lỗi liên tiếp -> blocked" -- and
# it is the exact mistake F4 exists to prevent: a private profile reported as
# `blocked` sends an operator to rotate a cookie that is fine.
# ############################################################################

run_mutation "a successful probe is treated as a GLOBAL fault (1a: the distinction)" \
  "$PROBE" "test/parser/f4-probe test/lanes/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/parser/f4-probe.ts")
s = p.read_text()
old = """    verdict: \x27cookie-healthy\x27,
    global: false,"""
new = """    verdict: \x27cookie-healthy\x27,
    global: true,"""
assert old in s, "the cookie-healthy verdict changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the lane parks everything at blocked, as IDEA.md §6.2 literally says (1b)" \
  "$LANE" "test/lanes/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """    if (!probe.global) {
      if (!(await this.giveUp(job, preamble + probe.reason + trailer))) return this.lost(job);
      return \x27failed\x27;
    }
    if (!(await this.park(job, preamble + probe.reason + trailer))) return this.lost(job);"""
new = """    if (!(await this.park(job, preamble + probe.reason + trailer))) return this.lost(job);"""
assert old in s, "diagnose() changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 4. "The checkpoint survives BOTH verdicts" could be carried by two
# different things: `markBlocked`/`markFailed` not touching `max_cursor`, or
# `checkpoint`s own monotonicity rule refusing to move it. Removing the
# monotonicity WHERE clause must leave the F4 checkpoint assertions GREEN --
# otherwise they are measuring phase 5's guard, not phase 7's writes.
run_control "checkpoint loses its monotonicity gate (F4 checkpoint claims must stand alone)" \
  "$JOBS_REPO" "test/lanes/f4-probe -t checkpoint" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """        lt(jobs.enumeratedCount, input.enumeratedCount),
      ),
    )
    .returning({ id: jobs.id });"""
new = """      ),
    )
    .returning({ id: jobs.id });"""
assert old in s, "the checkpoint WHERE clause changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 2 -- the probe's other four outcomes, which is where "invented success"
# would hide.
#
# `parser.client.ts` refuses to default a missing `status_code` to 0 ("inventing
# success that was never observed is the same mistake as inventing a cause for a
# 400"). Phase 7 is the consumer that makes that refusal load-bearing, so the
# two mutations below are the two ways to un-refuse it -- one at the client, one
# at the probe.
# ############################################################################

run_mutation "an unreadable probe is read as healthy (invents the success)" \
  "$PROBE" "test/lanes/f4-probe test/parser/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/parser/f4-probe.ts")
s = p.read_text()
old = """      verdict: \x27inconclusive\x27,
      global: true,"""
new = """      verdict: \x27inconclusive\x27,
      global: false,"""
assert old in s, "the inconclusive verdict changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "HTTP 200 is taken to mean Douyin agreed (drops the status_code check)" \
  "$PROBE" "test/parser/f4-probe test/lanes/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/parser/f4-probe.ts")
s = p.read_text()
old = "  if (profile.statusCode !== 0) {"
new = "  if (profile.statusCode !== 0 && false) {"
assert old in s, "the Douyin-refusal check changed shape"
p.write_text(s.replace(old, new, 1))
'

# The dependency the probe rests on, mutated at its own layer. `douyinStatusCode`
# returning 0 for an absent field would make every unreadable probe report a
# healthy cookie -- and the probe code above would be untouched and still
# correct, so no mutation of f4-probe.ts could ever find it.
run_mutation "the parser client invents status_code 0 when Douyin sent none" \
  "$PARSER_CLIENT" "test/parser/parser.client" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
old = """  if (typeof raw === \x27string\x27 && raw.trim() !== \x27\x27 && Number.isFinite(Number(raw))) return Number(raw);
  return null;"""
new = """  if (typeof raw === \x27string\x27 && raw.trim() !== \x27\x27 && Number.isFinite(Number(raw))) return Number(raw);
  return 0;"""
assert old in s, "douyinStatusCode changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "a non-parser error out of the probe is swallowed into a verdict" \
  "$PROBE" "test/parser/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/parser/f4-probe.ts")
s = p.read_text()
old = """    throw err;
  }"""
new = """    return {
      verdict: \x27inconclusive\x27,
      global: true,
      reason: \x27could not read the probe\x27,
    };
  }"""
assert old in s, "the probe rethrow changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 3 -- WHEN the probe runs. §7.1's whole economy: the probe is tier 3, so
# it costs nothing on the happy path and nothing on the structural signal.
# ############################################################################

run_mutation "probe on EVERY 400 instead of after three consecutive ones" \
  "$LANE" "test/lanes/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = "        if (rejections < MAX_CONSECUTIVE_REJECTIONS) {"
new = "        if (false && rejections < MAX_CONSECUTIVE_REJECTIONS) {"
assert old in s, "the retry-before-diagnose branch changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the 400 counter becomes cumulative instead of consecutive" \
  "$LANE" "test/lanes/f4-probe test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """        page = await this.parser.fetchUserPostPage(job.secUserId, cursor, ENUMERATE_PAGE_SIZE);
        rejections = 0;"""
new = """        page = await this.parser.fetchUserPostPage(job.secUserId, cursor, ENUMERATE_PAGE_SIZE);"""
assert old in s, "the rejection-counter reset changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "spend a probe on the structural signal, which is already certain" \
  "$LANE" "test/lanes/f4-probe test/lanes/cookie-recovery" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (page.awemeList.length === 0 && page.hasMore) {
        const parked = await this.park("""
new = """      if (page.awemeList.length === 0 && page.hasMore) {
        return this.diagnose(job, cursor);
        const parked = await this.park("""
assert old in s, "the structural blocked branch changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 4 -- the boot-time check on the probe TARGET (phase-07 Risks row 1).
#
# This is F4 inverted, and the nastiest property of it is that it is silent: a
# dead probe target makes every 3x400 resolve to `blocked`, which is exactly what
# a genuinely banned cookie looks like. The check is the only thing that can tell
# an operator otherwise.
# ############################################################################

run_mutation "the health check keys on 'did it throw?' rather than on the verdict" \
  "$PROBE_HEALTH" "test/parser/probe-health" \
  py '
import pathlib
p = pathlib.Path("src/parser/probe-health.ts")
s = p.read_text()
old = """      const result = await probeCookie(this.parser, probeSecUserId);
      if (!result.global) {"""
new = """      const result = await probeCookie(this.parser, probeSecUserId);
      if (result.verdict === \x27cookie-healthy\x27 || result.global) {"""
assert old in s, "the health-check verdict branch changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the boot check runs on API-only nodes too (drops the LANES_ENABLED gate)" \
  "$LANES_MODULE" "test/lanes/probe-health-wiring" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = "          enabled: env.LANES_ENABLED,"
new = "          enabled: true,"
assert old in s, "the ProbeHealthCheck factory changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the boot check never runs anywhere (hard-wired off)" \
  "$LANES_MODULE" "test/lanes/probe-health-wiring" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = "          enabled: env.LANES_ENABLED,"
new = "          enabled: false,"
assert old in s, "the ProbeHealthCheck factory changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 5 -- PHASE 6's HANDOVER, and the block worth reading twice.
#
# Before phase 7 this had NO GUARD, and its absence was invisible: cancel ->
# videos `skipped` -> nothing ever reset `skipped` -> the next job on the profile
# re-enumerated, `upsertMany` correctly did nothing (F7), `claimOne` never looks
# at `skipped`, and because `skipped` counts as SETTLED the job walked to
# `completed` with `downloaded_count = 0`. Every number agreed and the videos
# simply never arrived. Same family as phase 6's zero-byte commit.
#
# There are TWO independent ways back (retry the cancelled job; create a new
# one), so by rule 4 each needs to be shown to stand on its own -- see the
# control at the end of this block.
# ############################################################################

run_mutation "cancelled is not retryable again (the original dead end)" \
  "$JOBS_REPO" "test/db/cancel-retry-revival test/db/jobs.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = "const RETRYABLE_STATUSES = [\x27blocked\x27, \x27failed\x27, \x27cancelled\x27] as const;"
new = "const RETRYABLE_STATUSES = [\x27blocked\x27, \x27failed\x27] as const;"
assert old in s, "RETRYABLE_STATUSES changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the revival forgets to reset attempts (row reads pending, claim cannot see it)" \
  "$VIDEOS_REPO" "test/db/cancel-retry-revival" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """    .set({ status: \x27pending\x27, attempts: 0, leaseExpiresAt: null })
    .where(and(eq(videos.secUserId, secUserId), eq(videos.status, \x27skipped\x27)))"""
new = """    .set({ status: \x27pending\x27, leaseExpiresAt: null })
    .where(and(eq(videos.secUserId, secUserId), eq(videos.status, \x27skipped\x27)))"""
assert old in s, "reviveSkippedForProfile changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the revival sweeps every status, re-downloading the whole archive" \
  "$VIDEOS_REPO" "test/db/cancel-retry-revival" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """    .set({ status: \x27pending\x27, attempts: 0, leaseExpiresAt: null })
    .where(and(eq(videos.secUserId, secUserId), eq(videos.status, \x27skipped\x27)))"""
new = """    .set({ status: \x27pending\x27, attempts: 0, leaseExpiresAt: null })
    .where(eq(videos.secUserId, secUserId))"""
assert old in s, "reviveSkippedForProfile changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "a new job no longer revives the profile (the silent completed-with-0)" \
  "$JOBS_SERVICE" "test/jobs/jobs.service.revival" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.service.ts")
s = p.read_text()
old = "      await videosRepository.reviveSkippedForProfile(tx, secUserId);\n      return job;"
new = "      return job;"
assert old in s, "createFromUrl changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "/retry no longer revives skipped videos" \
  "$JOBS_SERVICE" "test/jobs/jobs.service.revival test/lanes/recovery-loop" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.service.ts")
s = p.read_text()
old = """      const skippedRevived = await videosRepository.reviveSkippedForProfile(tx, job.secUserId);
      return { job, videosReset: failedReset + skippedRevived };"""
new = """      return { job, videosReset: failedReset };"""
assert old in s, "JobsService.retry changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "videos_reset under-reports, so the API answer stops being confirmation" \
  "$JOBS_SERVICE" "test/jobs/jobs.service.revival" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.service.ts")
s = p.read_text()
old = "      return { job, videosReset: failedReset + skippedRevived };"
new = "      void skippedRevived;\n      return { job, videosReset: failedReset };"
assert old in s, "JobsService.retry changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the revival runs even when the retry did not land (re-skip loop)" \
  "$JOBS_SERVICE" "test/jobs/jobs.service.revival" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.service.ts")
s = p.read_text()
old = """      const job = await jobsRepository.retry(tx, id);
      if (!job) return null;
      const failedReset = await videosRepository.resetFailedForProfile(tx, job.secUserId);"""
new = """      const job = await jobsRepository.retry(tx, id);
      if (!job) {
        const [any] = await tx.select().from(schema.jobs).limit(1);
        if (any) await videosRepository.reviveSkippedForProfile(tx, any.secUserId);
        return null;
      }
      const failedReset = await videosRepository.resetFailedForProfile(tx, job.secUserId);"""
assert old in s, "JobsService.retry changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 4. TWO independent ways back exist, and a red mutation on either says
# only that "somebody can revive". Removing `cancelled` from RETRYABLE_STATUSES
# must leave the NEW-JOB path green -- that is what proves the createFromUrl
# revival is a way back in its own right and not a decoration on the retry path.
run_control "cancelled un-retryable (the new-job path must stand on its own)" \
  "$JOBS_REPO" "test/jobs/jobs.service.revival -t createFromUrl" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = "const RETRYABLE_STATUSES = [\x27blocked\x27, \x27failed\x27, \x27cancelled\x27] as const;"
new = "const RETRYABLE_STATUSES = [\x27blocked\x27, \x27failed\x27] as const;"
assert old in s, "RETRYABLE_STATUSES changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 6 -- §7.2, the recovery loop. The two ways to make a recovery expensive
# rather than wrong, which is why they need their own mutations: neither one
# breaks a status anywhere.
# ############################################################################

run_mutation "/retry throws the checkpoint away, re-paging the whole profile" \
  "$JOBS_REPO" "test/lanes/recovery-loop test/lanes/cookie-recovery" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """    .set({ status: \x27pending\x27, error: null, leaseExpiresAt: null, updatedAt: sql`now()` })
    .where(and(eq(jobs.id, id), inArray(jobs.status, RETRYABLE_STATUSES)))"""
new = """    .set({
      status: \x27pending\x27,
      error: null,
      leaseExpiresAt: null,
      maxCursor: 0,
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.id, id), inArray(jobs.status, RETRYABLE_STATUSES)))"""
assert old in s, "jobsRepository.retry changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "/retry resets DONE videos too, re-downloading the archive" \
  "$VIDEOS_REPO" "test/lanes/recovery-loop" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """    .set({ status: \x27pending\x27, attempts: 0, lastError: null, leaseExpiresAt: null })
    .where(and(eq(videos.secUserId, secUserId), eq(videos.status, \x27failed\x27)))"""
new = """    .set({ status: \x27pending\x27, attempts: 0, lastError: null, leaseExpiresAt: null })
    .where(and(eq(videos.secUserId, secUserId), inArray(videos.status, [\x27failed\x27, \x27done\x27])))"""
assert old in s, "resetFailedForProfile changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "a failed enumerate loses the checkpoint (F4 giveUp path)" \
  "$JOBS_REPO" "test/lanes/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """      status: \x27failed\x27,
      error: redactor.redact(reason),
      leaseExpiresAt: null,
      updatedAt: sql`now()`,"""
new = """      status: \x27failed\x27,
      error: redactor.redact(reason),
      leaseExpiresAt: null,
      maxCursor: 0,
      updatedAt: sql`now()`,"""
assert old in s, "jobsRepository.markFailed changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 6b -- THREE GUARDS ADDED BY THE ADVERSARIAL REVIEW OF THIS PHASE.
#
# Each is an error path that had no invariant watching it, which is the question
# phase 6 said to ask at the end of every phase. None of the three breaks a
# status anywhere, which is exactly why none of them would have been noticed.
# ############################################################################

# The probe is a 30s network call that does NOT renew the lease, so the reaper
# has a real window in which to hand the job away while we wait. `markBlocked` /
# `markFailed` are gated on `status = enumerating`, so the write then affects no
# row -- and phase 5's code logged "job X is BLOCKED" regardless, about a job
# that is `pending` under somebody else's claim.
run_mutation "the verdict writers ignore whether the write landed" \
  "$LANE" "test/lanes/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
before = s
s = s.replace("    return parked;\n  }", "    return true;\n  }", 1)
s = s.replace("    return failed;\n  }", "    return true;\n  }", 1)
assert s != before, "park/giveUp changed shape"
p.write_text(s)
'

# Nest awaits `onApplicationBootstrap`. Awaiting the probe there holds
# `app.init()` open for the parser's whole timeout whenever the parser is down --
# delaying /health and, through `depends_on: service_healthy`, the web container.
# The check was made non-fatal precisely so an unreachable Douyin could not stop
# the service from serving.
# `-t BOOTSTRAP`, not the whole file, and the reason is measured rather than
# stylistic: with this mutation applied the exploding-logger test in the same
# file gets a REJECTED promise out of a hook it does not await, which is an
# unhandled rejection -- so Node kills the jest worker and the whole suite
# reports no results at all. The harness correctly refuses to read that as a
# catch (rule 2/8), so the two mutations get one test each.
run_mutation "the boot check blocks bootstrap on the probe" \
  "$PROBE_HEALTH" "test/parser/probe-health -t BOOTSTRAP" \
  py '
import pathlib
p = pathlib.Path("src/parser/probe-health.ts")
s = p.read_text()
old = """  onApplicationBootstrap(): void {
    if (!this.enabled) return;"""
new = """  async onApplicationBootstrap(): Promise<void> {
    if (!this.enabled) return;
    await this.run();"""
assert old in s, "the bootstrap hook changed shape"
p.write_text(s.replace(old, new, 1))
'

# Nothing awaits `done` in production, so a rejection out of the check is an
# unhandled rejection -- i.e. a process crash caused by a diagnostic that was
# deliberately made non-fatal. Phase 4's review recorded the same shape: the
# outermost catch is not allowed to be the thing that breaks.
run_mutation "the boot check can reject into nothing (unhandled rejection)" \
  "$PROBE_HEALTH" "test/parser/probe-health -t rejects" \
  py '
import pathlib
p = pathlib.Path("src/parser/probe-health.ts")
s = p.read_text()
old = "    this.done = this.run().catch(() => undefined);"
new = "    this.done = this.run();"
assert old in s, "the bootstrap hook changed shape"
p.write_text(s.replace(old, new, 1))
'

# The race the revival alone cannot close: a lane holding a claim on a cancelled
# profile writes `skipped` AFTER a new job has been created and has already run
# its revival. The row is then `skipped` under a LIVE job, `claimOne` never looks
# at it, and because `skipped` counts as SETTLED the new job finalizes as
# `completed` with the video missing and every counter agreeing.
run_mutation "markSkipped no longer asks whether a live job wants the profile" \
  "$VIDEOS_REPO" "test/db/cancel-retry-revival" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = "    .where(and(ownsClaim(awemeId, attempts), notExists(liveJobForVideoProfile(exec))))"
new = "    .where(ownsClaim(awemeId, attempts))"
assert old in s, "markSkipped changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 4. "Refuses to skip once a live job exists" could be carried by the new
# predicate OR by `ownsClaim`. Weakening `ownsClaim` must leave the race test
# GREEN, which is what shows the new predicate is the mechanism.
run_control "ownsClaim loses its claim token (the live-job predicate must carry the race)" \
  "$VIDEOS_REPO" "test/db/cancel-retry-revival -t LIVE" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """    eq(videos.attempts, attempts),
    gt(videos.leaseExpiresAt, sql`now()`),"""
new = """    gt(videos.leaseExpiresAt, sql`now()`),"""
assert old in s, "ownsClaim changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 7 -- §7.3, the download phase's rate limit. MEASURED overlap, not a
# count of calls: `Promise.all` is parallel dispatch, not overlapping execution.
#
# Two mutations, because the DoD line has two halves -- "== DOWNLOAD_CONCURRENCY"
# and "không hơn" -- and one edit each is what tells them apart.
# ############################################################################

run_mutation "one download lane instead of DOWNLOAD_CONCURRENCY (the '==' half)" \
  "$LANES_MODULE" "test/lanes/download-concurrency" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = "          { length: env.DOWNLOAD_CONCURRENCY },"
new = "          { length: 1 },"
assert old in s, "the download-lane factory changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "two MORE download lanes than configured (the 'không hơn' half)" \
  "$LANES_MODULE" "test/lanes/download-concurrency" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = "          { length: env.DOWNLOAD_CONCURRENCY },"
new = "          { length: env.DOWNLOAD_CONCURRENCY + 2 },"
assert old in s, "the download-lane factory changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 4. "Exactly three in flight" could be held by the array length OR by the
# claim being exclusive (one video per lane). Renaming the lanes must leave the
# overlap measurement green -- which is what shows it measures CONCURRENCY rather
# than lane identity, and that the positive control's 5 is about loops and not
# about names.
run_control "renumber the download lanes (the meter must measure overlap, not names)" \
  "$LANES_MODULE" "test/lanes/download-concurrency" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = "              index: i + 1,"
new = "              index: i + 41,"
assert old in s, "the download-lane index changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# CONTROL -- the harness itself must be able to report GREEN.
#
# Every "caught" above is only meaningful if this run can also produce a pass. A
# harness whose every answer is "red" is indistinguishable from a broken build,
# and this repo has shipped that shape twice (plan §5, rules 2 and 3).
# ############################################################################

run_control "reword the FAILED log line (must change nothing observable)" \
  "$LANE" "test/lanes/f4-probe" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = "`lane ${this.name}: job ${job.id} FAILED (not blocked -- the probe proved the cookie is `"
new = "`lane ${this.name}: job ${job.id} GAVE UP (not blocked -- the probe proved the cookie is `"
assert old in s, "the giveUp log line changed shape"
p.write_text(s.replace(old, new, 1))
'

echo
echo "=============================================================="
if [ "$fail" -eq 0 ]; then
  echo "PHASE 7 MUTATIONS: every must-fail was caught, every control stayed green."
else
  echo "PHASE 7 MUTATIONS: FAILURES ABOVE. A mutation that stayed green is a guard"
  echo "nobody is watching; a control that went red means the invariant is resting"
  echo "on a different mechanism than its comment claims. Read plan.md §5."
fi
echo "=============================================================="
exit "$fail"

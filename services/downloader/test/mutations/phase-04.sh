#!/usr/bin/env bash
#
# Phase 4 mutation suite (plan.md §5, phase-04 DoD).
#
# "The suite is green" is evidence the suite RAN, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# named tests to go red. All four enforcement rules from plan §5 are carried
# over from phase-03.sh, because each of them exists because of a specific way
# a mutation harness has already lied in this repo:
#
#   1. Every mutation prints the diff it actually applied. A sed that matched
#      nothing is a mutation that never ran, and reporting that as a pass is how
#      a guard silently dies. Empty diff = hard error. (Phase 1, twice.)
#   2. A mutation is evidence only if the code changed AND tests actually ran.
#      "No tests found" is a hard error, not a signal in either direction.
#      (Phase 3: five must-fail mutations reported "caught" having run nothing.)
#   3. Every listed file must change, and the restore is verified with one
#      mechanism that works on untracked files. (Phase 3: `git checkout --` is a
#      no-op on a new file, so a mutation survived the rest of the run.)
#   4. When an invariant is held by MORE THAN ONE mechanism, a red mutation
#      proves only that the line matters somehow -- not which mechanism carries
#      it. Such invariants also get a CONTROL that must stay GREEN. (Phase 2.)
#
# Phase 4's headline invariant -- "a lane does not die silently" -- is exactly
# the kind that has no natural detector: nothing crashes, no healthcheck goes
# red, work just stops. That is why solution design §4 accepted A1's cost as
# "an obligation to catch everything, with a mutation test, not a reminder".
#
# THREE of the mutations here correspond to bugs this phase actually shipped and
# then found (each is marked FOUND-IN-PHASE-4). They are the reason the file is
# longer than the DoD's four lines: a mutation for a bug that was real is worth
# more than one for a bug that was imagined.
#
# Run from services/downloader:  bash test/mutations/phase-04.sh
# Needs the docker-compose stack up (Postgres + Redis).

set -u

cd "$(dirname "$0")/../.." || exit 1

RUNNER=src/lanes/lane.runner.ts
REAPER=src/lanes/reaper.lane.ts
SUPERVISOR=src/lanes/lane.supervisor.ts
BOOTSTRAP=src/bootstrap.ts
DB_MODULE=src/db/db.module.ts
JOBS_REPO=src/db/jobs.repository.ts
STORAGE=src/storage/local-disk.storage.ts
ENV_FILE=src/config/env.ts
JOBS_CTRL=src/jobs/jobs.controller.ts
LANES_MODULE=src/lanes/lanes.module.ts

BACKUP_DIR=$(mktemp -d)
fail=0

# ---------------------------------------------------------------------------
# RULE 5, added in phase 4: a harness that is KILLED must not leave the tree
# poisoned.
#
# Rules 1-4 all assume the harness reaches its own restore call. This one does
# not. Phase 4 learned it the hard way: a watchdog `pkill`ed the run between
# `_apply` and `restore`, one mutation stayed baked into an UNTRACKED new file
# (so `git checkout` had nothing to offer), and the next run measured a
# crippled build -- reporting the second mutation as "matched nothing" while
# every result after it was meaningless. The failure mode is the same one plan
# §5 keeps describing: the harness breaks by reporting confidence.
#
# Two halves. PRESERVE takes a pristine copy of every file this suite can touch
# before anything runs. The EXIT trap puts them all back, however the script
# ends -- normally, on error, or on a signal -- and says so, because a silent
# restore is indistinguishable from no restore.
# ---------------------------------------------------------------------------
PRISTINE_DIR=$(mktemp -d)
ALL_TARGETS="$RUNNER $REAPER $SUPERVISOR $BOOTSTRAP $DB_MODULE $JOBS_REPO $STORAGE $ENV_FILE $JOBS_CTRL $LANES_MODULE"
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
trap restore_all_from_pristine EXIT INT TERM

# Refuse to start from an already-modified tree. Every measurement below is
# relative to HEAD-plus-working-tree, so a leftover mutation makes the whole run
# a lie -- and the ONE thing this suite must never do is sound confident about
# a build nobody intended.
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

# _apply <label> <files> <mutator...> -- applies and refuses an empty diff.
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

# How long one mutation's jest run may take before it is called hung. The whole
# suite finishes in ~15s, so this is generous by an order of magnitude.
JEST_TIMEOUT_SECONDS=180

# Runs jest with <patterns> word-split into SEPARATE arguments.
# 0 = green, 1 = red, 98 = it never finished, 99 = the pattern selected nothing.
#
# RULE 6, added in phase 4: A MUTATION CAN HANG INSTEAD OF FAILING.
#
# The starvation mutation below (drop the loop's `setImmediate` yield) does not
# make tests fail, it makes the event loop stop turning -- so jest's own
# per-test timeout cannot fire either, and the run never ends. Without a bound
# here the suite stalls at that mutation forever and reports nothing about it or
# anything after it.
#
# A timeout is reported as its OWN outcome, never quietly folded into "caught".
# For a must-fail mutation a hang is a legitimate catch (the tests did not pass)
# and is labelled as such; for a control it is a failure. Treating a timeout as
# a generic red is how "Postgres is down" would come to look like sixteen
# successful mutations. `timeout(1)` is not on macOS, hence the background/kill
# dance.
run_jest() {
  # shellcheck disable=SC2086 -- word-splitting is the point (rule 2).
  pnpm test -- $1 >"$BACKUP_DIR/out.log" 2>&1 &
  local jest_pid=$!
  local waited=0
  while kill -0 "$jest_pid" 2>/dev/null; do
    if [ "$waited" -ge "$JEST_TIMEOUT_SECONDS" ]; then
      kill -9 "$jest_pid" 2>/dev/null
      # jest's workers are children of that shell and outlive a kill on it.
      pkill -9 -f "jest.js -- $1" 2>/dev/null
      wait "$jest_pid" 2>/dev/null
      return 98
    fi
    sleep 1
    waited=$((waited + 1))
  done
  wait "$jest_pid"
  local rc=$?
  if grep -q "No tests found" "$BACKUP_DIR/out.log"; then return 99; fi
  return "$rc"
}

report_no_tests() {
  echo "ERROR: the test pattern '$1' selected NO tests, so this mutation ran"
  echo "       against nothing. That is not a pass and not a failure -- it is a"
  echo "       broken mutation. Fix the pattern."
  fail=1
}

# run_mutation <label> <files> <test-pattern> <mutator...> -- tests MUST go red.
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
    # A hang is a genuine catch -- the tests did not pass -- but it is labelled,
    # because "it never finished" and "it failed an assertion" are different
    # facts and only one of them names the defect.
    echo "OK: caught, but by HANGING rather than failing (${JEST_TIMEOUT_SECONDS}s timeout)."
    echo "    A red test names the defect; a hang does not. Prefer a test that"
    echo "    can report this failure -- see MAX_SCRIPTED_TICKS in lane.runner.spec.ts."
  elif [ "$rc" -eq 0 ]; then
    echo "FAIL: suite stayed GREEN with the invariant broken -- these tests prove nothing."
    tail -20 "$BACKUP_DIR/out.log"
    fail=1
  else
    echo "OK: caught. Failing tests:"
    grep -E '✕' "$BACKUP_DIR/out.log" || tail -10 "$BACKUP_DIR/out.log"
  fi
  restore "$files"
}

# run_control <label> <files> <test-pattern> <mutator...> -- tests MUST stay green.
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
    grep -E '✕' "$BACKUP_DIR/out.log" || tail -20 "$BACKUP_DIR/out.log"
    fail=1
  fi
  restore "$files"
}

# run_lint_mutation <label> <files> <mutator...> -- `pnpm lint` MUST go red.
run_lint_mutation() {
  local label=$1 files=$2
  shift 2
  _apply "$label" "$files" "$@" || return
  echo "  check: pnpm lint (must FAIL)"
  if pnpm lint >"$BACKUP_DIR/out.log" 2>&1; then
    echo "FAIL: lint stayed GREEN -- the no-restricted-imports rule is not covering this path."
    tail -20 "$BACKUP_DIR/out.log"
    fail=1
  else
    echo "OK: caught by ESLint:"
    grep -E 'no-restricted-imports|lanes' "$BACKUP_DIR/out.log" | head -5 || tail -10 "$BACKUP_DIR/out.log"
  fi
  restore "$files"
}

py() { python3 -c "$1"; }

# ############################################################################
# BLOCK 1 -- THE guard. plan §5: "Loop rò exception không giết lane".
#
# This is the price solution design §4 named for choosing A1 over `@Interval`.
# An exception that escapes the loop does not crash the process, does not fail
# the container healthcheck, and does not alert. The lane stops and every job
# it owned sits at `pending` forever.
# ############################################################################

run_mutation "remove the try/catch from the loop body (THE phase-4 guard)" \
  "$RUNNER" "test/lanes/lane.runner" \
  py '
import pathlib, re
p = pathlib.Path("src/lanes/lane.runner.ts")
s = p.read_text()
# Literally delete the inner try/catch: keep the tick, drop the handler.
start = s.index("      while (!signal.aborted) {")
end = s.index("    } catch (err: unknown) {\n      // Record FIRST")
body = s[start:end]
mutated = """      while (!signal.aborted) {
        const outcome = await this.runOnce(signal);
        this.tickAt = new Date();
        this.consecutiveFailures = 0;
        if (outcome === \x27idle\x27) await sleep(this.idleDelayMs, signal);
        else await yieldToEventLoop();
      }
"""
s = s[:start] + mutated + s[end:]
p.write_text(s)
'

# FOUND-IN-PHASE-4. The first run of lane.runner.spec.ts did not fail, it HUNG:
# a `worked` tick that resolves without touching I/O only ever yields to the
# MICROtask queue, so timers, sockets and signal handlers never get a turn. The
# API and the lanes share one process by design, so a starving lane means
# `/health` stops answering and Nest's SIGTERM handler never runs.
run_mutation "drop the setImmediate yield after a tick that worked (event-loop starvation)" \
  "$RUNNER" "test/lanes/lane.runner" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lane.runner.ts")
s = p.read_text()
s = s.replace("          else await yieldToEventLoop();", "          else await Promise.resolve();")
p.write_text(s)
'

run_mutation "log the tick failure but do not back off (hot error loop)" \
  "$RUNNER" "test/lanes/lane.runner" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lane.runner.ts")
s = p.read_text()
s = s.replace("          this.consecutiveFailures += 1;\n          await sleep(waitMs, signal);",
              "          await sleep(0, signal);")
p.write_text(s)
'

# ############################################################################
# BLOCK 2 -- plan §5: "Reaper nhặt lại video mồ côi" (F3) and "...job mồ côi"
# (F5). F5 is the one brainstorm missed entirely -- plan §2 calls it a bug if
# left to the end, because without it "kill the worker, watch it resume" is
# simply not true of the enumerate phase.
# ############################################################################

run_mutation "remove the reaper's videos sweep (F3)" \
  "$REAPER" "test/lanes/reaper" \
  py '
import pathlib, re
p = pathlib.Path("src/lanes/reaper.lane.ts")
s = p.read_text()
s = re.sub(
  r"    const reapedVideos = await this\.attempt\(.*?\n    \);\n",
  "    const reapedVideos: undefined = undefined;\n",
  s, count=1, flags=re.S)
p.write_text(s)
'

run_mutation "remove the reaper's jobs sweep (F5 -- the one brainstorm missed)" \
  "$REAPER" "test/lanes/reaper" \
  py '
import pathlib, re
p = pathlib.Path("src/lanes/reaper.lane.ts")
s = p.read_text()
s = re.sub(
  r"    const reapedJobs = await this\.attempt\(.*?\n    \);\n",
  "    const reapedJobs: undefined = undefined;\n",
  s, count=1, flags=re.S)
p.write_text(s)
'

run_mutation "remove the reaper's stale-.tmp sweep (plan §6 risk register)" \
  "$REAPER" "test/lanes/reaper test/storage/" \
  py '
import pathlib, re
p = pathlib.Path("src/lanes/reaper.lane.ts")
s = p.read_text()
s = re.sub(
  r"    const removed = await this\.attempt\(.*?\n    \);\n",
  "    const removed: undefined = undefined;\n",
  s, count=1, flags=re.S)
p.write_text(s)
'

# One broken sweep must not starve the other three -- the phase theme, one
# level down. A plain sequence of awaits means a first sweep that fails every
# tick stops the rest happening at all, forever, indistinguishably from health.
run_mutation "short-circuit the reaper on the first failed sweep" \
  "$REAPER" "test/lanes/reaper" \
  py '
import pathlib
p = pathlib.Path("src/lanes/reaper.lane.ts")
s = p.read_text()
s = s.replace("    if (signal.aborted) return undefined;\n    try {\n      return await run();",
              "    if (signal.aborted) return undefined;\n    if (failures.length > 0) return undefined;\n    try {\n      return await run();")
p.write_text(s)
'

# ############################################################################
# BLOCK 3 -- §4.2, "nhả lease": the DoD line is "restart nhặt lại NGAY (không
# chờ 5 phút)". Every mutation here leaves the system CORRECT-eventually and
# only breaks the "immediately" -- which is exactly why they need a test that
# measures immediacy rather than recovery.
# ############################################################################

run_mutation "stop() drains but never releases the held claim" \
  "$RUNNER" "test/lanes/lane.runner test/lanes/sigterm" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lane.runner.ts")
s = p.read_text()
s = s.replace("    await this.releaseHeld();\n  }", "  }", 1)
p.write_text(s)
'

# FOUND-IN-PHASE-4, and the most valuable mutation in this file: it is a real
# bug that shipped and was caught only by the child-process test. Nest runs
# every `onModuleDestroy` BEFORE every `beforeApplicationShutdown`, so a pool
# closed in the former is already gone when a lane tries to release. Every
# in-process shutdown test stayed green throughout, because none of them asks
# whether the release SUCCEEDED.
run_mutation "close the pg pool in onModuleDestroy again, before lanes release" \
  "$DB_MODULE" "test/lanes/sigterm" \
  py '
import pathlib
p = pathlib.Path("src/db/db.module.ts")
s = p.read_text()
s = s.replace("  type OnApplicationShutdown,", "  type OnModuleDestroy,")
s = s.replace("export class DbModule implements OnModuleInit, OnApplicationShutdown {",
              "export class DbModule implements OnModuleInit, OnModuleDestroy {")
s = s.replace("  async onApplicationShutdown(): Promise<void> {\n    await this.pool.end();",
              "  async onModuleDestroy(): Promise<void> {\n    await this.pool.end();")
p.write_text(s)
'

run_mutation "never install the signal handlers (drop app.enableShutdownHooks)" \
  "$BOOTSTRAP" "test/lanes/sigterm" \
  py '
import pathlib
p = pathlib.Path("src/bootstrap.ts")
s = p.read_text()
# Targets ONLY the enableShutdownHooks line, not the whole function body: the
# body now also configures CORS, and matching the entire function meant this
# mutation broke (loudly, via the unchanged-file guard of _apply) the moment
# anything else was added to it. One invariant, one line.
old = "  app.enableShutdownHooks();\n"
assert s.count(old) == 1, f"expected exactly 1 enableShutdownHooks call, found {s.count(old)}"
s = s.replace(old, "  // mutated: no signal handlers installed\n")
p.write_text(s)
'

run_mutation "shut lanes down one after another instead of concurrently" \
  "$SUPERVISOR" "test/lanes/lane.supervisor" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lane.supervisor.ts")
s = p.read_text()
s = s.replace("""    await Promise.all(
      this.lanes.map(async (lane) => {
        try {
          await lane.stop();
        } catch (err: unknown) {
          this.logger.error(`lane ${lane.name} failed to stop cleanly: ${describe(err)}`);
        }
      }),
    );""",
"""    for (const lane of this.lanes) {
      try {
        await lane.stop();
      } catch (err: unknown) {
        this.logger.error(`lane ${lane.name} failed to stop cleanly: ${describe(err)}`);
      }
    }""")
p.write_text(s)
'

# ############################################################################
# BLOCK 4 -- plan §5 rule 4. "An orphaned video comes back" is held by TWO
# mechanisms: the reaper (eventually) and the shutdown release (immediately).
# A red mutation on either says only that the line matters somehow. These two
# controls say WHICH mechanism each test is actually measuring.
# ############################################################################

# Kill the reaper's video sweep and require the SIGTERM immediacy test to stay
# green. If it went red, that test would be measuring the reaper, not the
# release -- and the "not in 5 minutes" half of the DoD would be unproven.
run_control "remove the reaper's videos sweep; the SIGTERM release test must NOT depend on it" \
  "$REAPER" "test/lanes/sigterm" \
  py '
import pathlib, re
p = pathlib.Path("src/lanes/reaper.lane.ts")
s = p.read_text()
s = re.sub(
  r"    const reapedVideos = await this\.attempt\(.*?\n    \);\n",
  "    const reapedVideos: undefined = undefined;\n",
  s, count=1, flags=re.S)
p.write_text(s)
'

# The mirror image: kill the shutdown release and require the reaper's own
# tests to stay green, so those are measuring the reaper rather than quietly
# resting on a release that happened first.
run_control "remove the shutdown release; the reaper's own tests must NOT depend on it" \
  "$RUNNER" "test/lanes/reaper" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lane.runner.ts")
s = p.read_text()
s = s.replace("    await this.releaseHeld();\n  }", "  }", 1)
p.write_text(s)
'

# ############################################################################
# BLOCK 5 -- the sweeps' own predicates. Each of these leaves the reaper
# running and looking healthy while doing the wrong thing.
# ############################################################################

run_mutation "finalize jobs in ANY status, not only downloading" \
  "$JOBS_REPO" "test/db/jobs.repository.finalize test/lanes/reaper" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
# Phase 6 gave the WHERE clause an optional profile scope, so the one-line
# form this used to match is gone. A mutation whose text no longer occurs
# matches NOTHING, which `_apply` treats as a hard error rather than a pass
# (rule 1) -- keeping this string in step with the query is the whole reason
# that rule exists.
old = """      and(
        eq(jobs.status, \x27downloading\x27),
        notExists(outstanding),
        ...(opts.secUserId === undefined ? [] : [eq(jobs.secUserId, opts.secUserId)]),
      ),"""
new = """      and(
        notExists(outstanding),
        ...(opts.secUserId === undefined ? [] : [eq(jobs.secUserId, opts.secUserId)]),
      ),"""
assert old in s, "the finalizeSettled WHERE clause changed shape"
s = s.replace(old, new, 1)
p.write_text(s)
'

run_mutation "finalize a job while videos are still outstanding" \
  "$JOBS_REPO" "test/db/jobs.repository.finalize test/lanes/reaper" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """      and(
        eq(jobs.status, \x27downloading\x27),
        notExists(outstanding),
        ...(opts.secUserId === undefined ? [] : [eq(jobs.secUserId, opts.secUserId)]),
      ),"""
new = """      and(
        eq(jobs.status, \x27downloading\x27),
        ...(opts.secUserId === undefined ? [] : [eq(jobs.secUserId, opts.secUserId)]),
      ),"""
assert old in s, "the finalizeSettled WHERE clause changed shape"
s = s.replace(old, new, 1)
p.write_text(s)
'

run_mutation "sweep .tmp files by NAME alone, ignoring the mtime cutoff" \
  "$STORAGE" "test/storage/ test/lanes/reaper" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("        if (info.mtimeMs > cutoff) continue;", "        void info;")
p.write_text(s)
'

# The in-flight registry is the guard that makes a cutoff-0 sweep safe. Without
# it, an aggressive manual cleanup unlinks a live download s temp file and that
# download fails its rename with ENOENT -- the failure phase 2 s journal spent a
# day on.
run_mutation "sweep .tmp files without consulting the in-flight registry" \
  "$STORAGE" "test/storage/" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("      if (this.inFlight.has(path)) continue;", "")
p.write_text(s)
'

# ############################################################################
# BLOCK 6 -- LANES_ENABLED. A flag whose whole job is turning the lanes OFF
# must not be readable as `true` when it says "false".
# ############################################################################

run_mutation "read LANES_ENABLED with z.coerce.boolean() (the string \"false\" becomes true)" \
  "$ENV_FILE" "test/env.spec" \
  py '
import pathlib
p = pathlib.Path("src/config/env.ts")
s = p.read_text()
s = s.replace("    LANES_ENABLED: booleanFlag(\"LANES_ENABLED\").default(true),",
              "    LANES_ENABLED: z.coerce.boolean().default(true),")
p.write_text(s)
'

run_mutation "start lanes regardless of LANES_ENABLED" \
  "$SUPERVISOR" "test/lanes/lane.supervisor" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lane.supervisor.ts")
s = p.read_text()
s = s.replace("    if (!this.env.LANES_ENABLED) {", "    if (false) {")
p.write_text(s)
'

# The wiring itself. `LANES` is a string token filled by a factory, so a
# dropped entry leaves a process that boots healthy, answers /health, and reaps
# nothing ever -- the same silent-nothing-happens shape as everything else here,
# except that no behavioural test would notice because there is no lane to
# behave.
run_mutation "hand the supervisor an EMPTY lane array" \
  "$LANES_MODULE" "test/lanes/lanes.module" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
# The factory grew an argument in phase 5 (enumerate) and another in phase 6
# (the download lanes). A mutation whose `old` text no longer exists matches
# NOTHING, and `_apply` reports that as a hard error rather than a pass -- rule
# 1. That is why this string has to be kept in step with the module: a stale
# mutation is a guard nobody is watching, and it fails silently in the one
# direction that looks like success.
old = """      useFactory: (
        reaper: ReaperLane,
        enumerate: EnumerateLane,
        downloads: DownloadLane[],
      ): LaneRunner[] => [reaper, enumerate, ...downloads],"""
new = """      useFactory: (
        reaper: ReaperLane,
        enumerate: EnumerateLane,
        downloads: DownloadLane[],
      ): LaneRunner[] => {
        void reaper;
        void enumerate;
        void downloads;
        return [];
      },"""
assert old in s, "LANES factory shape changed"
s = s.replace(old, new, 1)
p.write_text(s)
'

# ############################################################################
# BLOCK 7 -- plan §0 principle 1 / phase-04 DoD last line: "cho lane-module
# gọi trực tiếp từ controller → test đỏ". TWO enforcement layers, and each
# mutation is shaped to slip past the other one.
# ############################################################################

# Layer 1: ESLint, at edit time. Now aimed at a lane that REALLY EXISTS and
# from a CONTROLLER, which is the file the DoD names.
run_lint_mutation "jobs.controller takes a STATIC import on a real lane (ESLint layer)" \
  "$JOBS_CTRL" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.controller.ts")
s = p.read_text()
p.write_text("import \x27../lanes/reaper.lane\x27;\n" + s)
'

# Layer 2: the module-graph proof, and this is the mutation that finally makes
# it load-bearing.
#
# Phase 3 already had a "dynamic import" mutation here and it went red -- but it
# was caught by the REGEX scan, not by require.cache: the import expression was
# never called, so nothing was ever loaded. This one is built to defeat a text
# scan and cannot defeat the module registry: the path is assembled by
# concatenation so no string literal containing "lanes" sits next to `require(`,
# and the call is at module scope so requiring the controller RUNS it. A regex
# cannot see it; require.cache cannot miss it.
run_mutation "jobs.controller require()s a lane via a CONCATENATED path (module-graph layer)" \
  "$JOBS_CTRL" "test/architecture/" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.controller.ts")
s = p.read_text()
s += """
// eslint-disable-next-line @typescript-eslint/no-require-imports
const __laneModulePath = \x27../la\x27 + \x27nes/reaper.lane\x27;
// eslint-disable-next-line @typescript-eslint/no-require-imports
require(__laneModulePath);
"""
p.write_text(s)
'

echo
if [ "$fail" -eq 0 ]; then
  echo "All phase-4 mutations applied; every mutation was caught and every control stayed green."
else
  echo "PHASE 4 MUTATION SUITE FAILED -- see above."
fi
exit "$fail"

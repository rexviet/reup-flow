#!/usr/bin/env bash
#
# Phase 5 mutation suite (plan.md §5, phase-05 DoD).
#
# "The suite is green" is evidence the suite RAN, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# named tests to go red. All six enforcement rules from plan §5 are carried over
# from phase-04.sh, because each exists because of a specific way a mutation
# harness has already lied in this repo:
#
#   1. Every mutation prints the diff it actually applied. Empty diff = hard
#      error, never a pass. (Phase 1, twice.)
#   2. A mutation is evidence only if the code changed AND tests actually ran.
#      "No tests found" is a hard error in both directions. (Phase 3, five
#      must-fail mutations reported "caught" having run nothing.)
#   3. Every listed file must change, and the restore is verified with one
#      mechanism that works on untracked files. (Phase 3: `git checkout --` is a
#      no-op on a new file.)
#   4. When an invariant is held by MORE THAN ONE mechanism, a red mutation
#      proves only that the line matters somehow -- not which mechanism carries
#      it. Such invariants also get a CONTROL that must stay GREEN. (Phase 2.)
#   5. A harness that is KILLED must not leave the tree poisoned: pristine
#      snapshot + `trap ... EXIT INT TERM`, and it SAYS SO when it restores.
#      (Phase 4.)
#   6. A mutation can HANG instead of failing. A timeout is its own outcome:
#      a legitimate catch for a must-fail, a FAILURE for a control. (Phase 4.)
#
# Phase 5's three DoD mutations are BLOCK 1, 2 and 3. Everything after them is
# a guard this phase added on its own, and BLOCK 4 is the one that matters most:
# it corresponds to a bug PHASE 4 SHIPPED and phase 5 found. A mutation for a
# bug that was real is worth more than one for a bug that was imagined.
#
# BLOCK 12 was added 2026-08-26, long after the phase closed, for a second bug
# that SHIPPED: 图文 image posts have no video, `video.play_addr` points at the
# background music CDN, and the mapper never asked -- so two of a 215-item
# profile were downloaded as MP3s, named `.mp4` and marked `done` with every
# counter agreeing. It lives here rather than in a harness of its own because
# `aweme.mapper.ts` is this phase's file, and a guard measured by a script CI
# does not run is a guard nobody is watching (see scripts/run-mutations.sh).
#
# Run from services/downloader:  bash test/mutations/phase-05.sh
# Needs the docker-compose stack up (Postgres + Redis).

set -u

cd "$(dirname "$0")/../.." || exit 1

LANE=src/lanes/enumerate.lane.ts
LANES_MODULE=src/lanes/lanes.module.ts
JOBS_REPO=src/db/jobs.repository.ts
VIDEOS_REPO=src/db/videos.repository.ts
MAPPER=src/lanes/aweme.mapper.ts

BACKUP_DIR=$(mktemp -d)
fail=0

# --- RULE 5 -------------------------------------------------------------------
PRISTINE_DIR=$(mktemp -d)
ALL_TARGETS="$LANE $LANES_MODULE $JOBS_REPO $VIDEOS_REPO $MAPPER"
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

# The F2 rate suite is the slowest here at ~5s. 240s is generous by two orders.
JEST_TIMEOUT_SECONDS=240

# 0 = green, 1 = red, 98 = never finished, 99 = the pattern selected nothing.
run_jest() {
  # shellcheck disable=SC2086 -- word-splitting is the point (rule 2).
  pnpm test -- $1 >"$BACKUP_DIR/out.log" 2>&1 &
  local jest_pid=$!
  local waited=0
  while kill -0 "$jest_pid" 2>/dev/null; do
    if [ "$waited" -ge "$JEST_TIMEOUT_SECONDS" ]; then
      kill -9 "$jest_pid" 2>/dev/null
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
# BLOCK 1 -- plan §5: "Enumerate global 1 luồng (F2) -- đổi thành per-job --
# test tần suất 2 job ĐỎ".
#
# Douyin rate-limits by IP, not by job. This is the mutation the DoD names, and
# it is applied where F2 actually lives: the number of instances in the LANES
# array, not any line inside the lane.
# ############################################################################

run_mutation "register TWO enumerate lanes (F2 -> per-job concurrency)" \
  "$LANES_MODULE" "test/lanes/enumerate-global-concurrency" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = """      useFactory: (
        reaper: ReaperLane,
        enumerate: EnumerateLane,
        downloads: DownloadLane[],
      ): LaneRunner[] => [reaper, enumerate, ...downloads],"""
new = """      useFactory: (
        reaper: ReaperLane,
        enumerate: EnumerateLane,
        downloads: DownloadLane[],
      ): LaneRunner[] => [
        reaper,
        enumerate,
        new EnumerateLane(enumerate.logger, enumerate[\x27db\x27], enumerate[\x27parser\x27], enumerate[\x27opts\x27]),
        ...downloads,
      ],"""
assert old in s, "LANES factory shape changed"
p.write_text(s.replace(old, new, 1))
'

# RULE 4 CONTROL. A red above proves the rate suite noticed THE ARRAY CHANGED.
# It does not prove the suite noticed the ENUMERATE concurrency changed -- and
# those are different claims. Adding a second REAPER changes the array in the
# same way and must leave the enumerate request rate untouched. If this goes
# red, the rate assertion is measuring array length, not requests.
run_control "register a second REAPER (the enumerate rate must not care)" \
  "$LANES_MODULE" "test/lanes/enumerate-global-concurrency" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = """      useFactory: (
        reaper: ReaperLane,
        enumerate: EnumerateLane,
        downloads: DownloadLane[],
      ): LaneRunner[] => [reaper, enumerate, ...downloads],"""
new = """      useFactory: (
        reaper: ReaperLane,
        enumerate: EnumerateLane,
        downloads: DownloadLane[],
      ): LaneRunner[] => [
        reaper,
        new ReaperLane(reaper.logger, reaper[\x27db\x27], reaper[\x27storage\x27], reaper[\x27opts\x27]),
        enumerate,
        ...downloads,
      ],"""
assert old in s, "LANES factory shape changed"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 2 -- plan §5: "Checkpoint mỗi trang -- chuyển checkpoint ra sau vòng lặp
# -- test resume giữa enumerate ĐỎ".
#
# A crash at page fourteen must cost fourteen pages, not the whole profile.
# ############################################################################

run_mutation "move the checkpoint OUT of the paging loop (checkpoint once, at the end)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      const stillOurs = await jobsRepository.checkpoint(this.db, job.id, {
        maxCursor: cursor,
        enumeratedCount: enumerated,
        leaseSeconds: this.opts.leaseSeconds,
      });
      if (!stillOurs) {"""
new = """      const stillOurs = true;
      if (!stillOurs) {"""
assert old in s, "checkpoint call shape changed"
s = s.replace(old, new, 1)
old2 = """    const finished = await jobsRepository.finishEnumerate(this.db, job.id);"""
new2 = """    await jobsRepository.checkpoint(this.db, job.id, {
      maxCursor: cursor,
      enumeratedCount: enumerated,
      leaseSeconds: this.opts.leaseSeconds,
    });
    const finished = await jobsRepository.finishEnumerate(this.db, job.id);"""
assert old2 in s
p.write_text(s.replace(old2, new2, 1))
'

# ############################################################################
# BLOCK 3 -- plan §5: "`blocked` ≠ `failed` -- map `blocked` → `failed` -- test
# hồi phục cookie ĐỎ".
#
# Both statuses are retryable, so the WORD is the whole remediation signal:
# `blocked` sends an operator to rotate the cookie, `failed` tells them the
# profile is not gettable. See the header of cookie-recovery.spec.ts for why the
# obvious version of that test would have stayed green here.
# ############################################################################

run_mutation "map blocked -> failed in markBlocked" \
  "$JOBS_REPO" "test/lanes/cookie-recovery" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """    .set({
      status: \x27blocked\x27,
      error: redactor.redact(reason),"""
new = """    .set({
      status: \x27failed\x27,
      error: redactor.redact(reason),"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# RULE 4 CONTROL. `markBlocked` writes both a status and a reason, and the lane
# also logs a line. A red above must come from the STATUS, not from the prose --
# otherwise the recovery test is pinning a log message. Rewording the log (and
# the reason) must leave it green.
run_control "reword the blocked log line and reason prose" \
  "$LANE" "test/lanes/cookie-recovery" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """    this.logger.warn(`lane ${this.name}: job ${job.id} is BLOCKED (not failed): ${reason}`);"""
new = """    this.logger.warn(`lane ${this.name}: parked ${job.id} -- see status`);"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 4 -- FOUND-IN-PHASE-5, and it was a bug PHASE 4 SHIPPED.
#
# `checkpoint` guarded against a straggler rewinding progress with
# `max_cursor = greatest(old, new)` -- monotonic in the cursor's NUMERIC value.
# Douyin's cursor is the create_time of the oldest item in the page and a
# profile pages newest-first, so the cursor DROPS every page: under GREATEST the
# checkpoint freezes at page one while `enumerated_count` keeps climbing and the
# lease keeps renewing. Everything observable looks healthy and every restart
# re-pages the profile from page two.
#
# No test in the repo could have caught it, because a fake parser is free to
# return ascending numbers. The fix was to gate the cursor on `enumerated_count`
# -- which only ever grows, whichever way Douyin's cursor moves -- and the fixed
# tests page DOWNWARDS, as the real thing does.
# ############################################################################

run_mutation "put GREATEST back on max_cursor (the phase-4 bug this phase found)" \
  "$JOBS_REPO" "test/db/jobs.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """      maxCursor: sql`CASE WHEN ${input.enumeratedCount} > ${jobs.enumeratedCount}
        THEN ${input.maxCursor}::bigint
        ELSE ${jobs.maxCursor} END`,"""
new = """      maxCursor: sql`greatest(${jobs.maxCursor}, ${input.maxCursor})`,"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# And the same mutation measured through the LANE, because a repository test
# passing descending numbers by hand is not the same evidence as a lane that
# pages a profile the way Douyin serves it.
run_mutation "put GREATEST back on max_cursor -- observed through the lane" \
  "$JOBS_REPO" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """      maxCursor: sql`CASE WHEN ${input.enumeratedCount} > ${jobs.enumeratedCount}
        THEN ${input.maxCursor}::bigint
        ELSE ${jobs.maxCursor} END`,"""
new = """      maxCursor: sql`greatest(${jobs.maxCursor}, ${input.maxCursor})`,"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 5 -- F1: the play_addr cache, and the dedupe that makes re-runs free.
# ############################################################################

run_mutation "stop caching play_addr/url_expires_at at enumerate time (F1)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      const rows = page.awemeList.map((item) => toVideoUpsert(item, job.secUserId));"""
new = """      const rows = page.awemeList
        .map((item) => toVideoUpsert(item, job.secUserId))
        .map((row) => (row === null ? null : { ...row, playAddr: null }));"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "turn ON CONFLICT DO NOTHING into an overwrite (F7 dedupe)" \
  "$VIDEOS_REPO" "test/db/videos.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """    .onConflictDoNothing()
    .returning({ awemeId: videos.awemeId });"""
new = """    .onConflictDoUpdate({
      target: videos.awemeId,
      set: { status: \x27pending\x27, attempts: 0, storagePath: null },
    })
    .returning({ awemeId: videos.awemeId });"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "compute url_expires_at from the APP clock instead of the DB clock" \
  "$VIDEOS_REPO" "test/db/videos.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """        urlExpiresAt: sql`now() + make_interval(secs => ${opts.urlTtlSeconds})`,"""
new = """        urlExpiresAt: new Date(0),"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 6 -- the pacing. phase-05 risk register: "`sleep` bị hoisted ra ngoài
# loop -- test đo khoảng cách giữa các request, không chỉ tổng thời gian".
# ############################################################################

run_mutation "hoist the sleep OUT of the paging loop (one wait, then a burst)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (!page.hasMore) break;

      // LAST in the body, so no delay follows the final page. INSIDE the loop,
      // so it paces requests rather than ticks -- the thing a scheduler
      // interval one layer up cannot express, and reason #1 for A1.
      await this.pause(signal);"""
new = """      if (!page.hasMore) break;"""
assert old in s
s = s.replace(old, new, 1)
# Anchored on the LOOP, not on the last counter declared above it. It used to
# read `let dropped = 0;` + the loop opener, and BLOCK 12 adding an `imagePosts`
# counter between the two made this mutation match nothing -- reported honestly,
# but it is the third time a phase-5 anchor has gone stale on an unrelated edit
# (see scripts/run-mutations.sh on why that is a build failure and not a smaller
# number). The loop and the abort check are what the mutation is actually about.
old2 = """    for (;;) {
      if (signal.aborted) {"""
new2 = """    // Hoisted: one wait for the whole job, which is what an @Interval one layer
    // up could express.
    await this.pause(signal);

    for (;;) {
      if (signal.aborted) {"""
assert old2 in s
p.write_text(s.replace(old2, new2, 1))
'

# ############################################################################
# BLOCK 7 -- §5.3, the structural blocked signal, and the refusal to diagnose.
# ############################################################################

run_mutation "ignore the structural blocked signal (empty page + has_more)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (page.awemeList.length === 0 && page.hasMore) {"""
new = """      if (false && page.awemeList.length === 0 && page.hasMore) {"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "treat ANY parser refusal as a cookie problem, not just a 400 (F4)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """        if (!(err instanceof ParserRejectedError) || err.status !== 400) throw err;"""
new = """        if (!(err instanceof ParserRejectedError)) throw err;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "park at blocked on the FIRST 400 instead of the third" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """export const MAX_CONSECUTIVE_REJECTIONS = 3;"""
new = """export const MAX_CONSECUTIVE_REJECTIONS = 1;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "count 400s cumulatively instead of consecutively" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """        page = await this.parser.fetchUserPostPage(job.secUserId, cursor, ENUMERATE_PAGE_SIZE);
        rejections = 0;"""
new = """        page = await this.parser.fetchUserPostPage(job.secUserId, cursor, ENUMERATE_PAGE_SIZE);"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 8 -- letting go of a claim, and the guards on the request budget.
# ############################################################################

run_mutation "keep the claim when the tick throws (leave it to the reaper)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      await release().catch((releaseErr: unknown) => {"""
new = """      await Promise.resolve().catch((releaseErr: unknown) => {"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "carry on paging a job that is no longer ours (checkpoint said false)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (!stillOurs) {
        // Reaped, cancelled, or completed by somebody else while we were
        // paging. It may already belong to another lane, so the one thing not
        // to do is hand it back underneath them.
        this.logger.warn(
          `lane ${this.name}: job ${job.id} is no longer ours after ${pages} page(s); stopping`,
        );
        return \x27lost\x27;
      }"""
new = """      void stillOurs;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "follow a cursor that does not advance (infinite paging loop)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (page.hasMore && cursor !== 0 && nextCursor >= cursor) {"""
new = """      if (false) {"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "ignore the shutdown signal mid-enumerate (§4.2)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (signal.aborted) {"""
new = """      if (false && signal.aborted) {"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 9 -- total_videos, and the mapper's refusal to invent data.
# ############################################################################

run_mutation "trust the lane page-sum for total_videos instead of counting rows" \
  "$JOBS_REPO" "test/db/jobs.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """      totalVideos: sql<number>`(
        select count(*) from ${videos} where ${videos.secUserId} = ${jobs.secUserId}
      )`,
      leaseExpiresAt: null,
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.id, jobId), eq(jobs.status, \x27enumerating\x27)))
    .returning({ id: jobs.id });

  return updated.length > 0;
}

/**
 * The cookie is being refused"""
new = """      totalVideos: jobs.enumeratedCount,
      leaseExpiresAt: null,
      updatedAt: sql`now()`,
    })
    .where(and(eq(jobs.id, jobId), eq(jobs.status, \x27enumerating\x27)))
    .returning({ id: jobs.id });

  return updated.length > 0;
}

/**
 * The cookie is being refused"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "default a missing create_time to now() instead of dropping the item" \
  "$MAPPER" "test/lanes/aweme.mapper" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """  const createTimeSeconds = numeric(aweme.create_time);
  if (createTimeSeconds === null || createTimeSeconds <= 0) return null;"""
new = """  const createTimeSeconds = numeric(aweme.create_time) ?? Math.floor(Date.now() / 1000);"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "read create_time as milliseconds instead of seconds" \
  "$MAPPER" "test/lanes/aweme.mapper" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """    createTime: new Date(createTimeSeconds * 1000),"""
new = """    createTime: new Date(createTimeSeconds),"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "drop an item whose play_addr is missing, instead of nulling the column" \
  "$MAPPER" "test/lanes/aweme.mapper" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """  const urls = playAddrUrls(aweme);"""
new = """  const urls = playAddrUrls(aweme);
  if (urls.length === 0) return null;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 10 -- the wiring, and the empty-input shapes Postgres rejects outright.
# ############################################################################

run_mutation "drop the enumerate lane from the LANES array entirely" \
  "$LANES_MODULE" "test/lanes/lanes.module" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
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
        void enumerate;
        return [reaper, ...downloads];
      },"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation 'let an empty page reach Postgres as values ()' \
  "$VIDEOS_REPO" "test/db/videos.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """  if (rows.length === 0) return 0;"""
new = """  if (false) return 0;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 11 -- added after the adversarial review, which PROVED four of this
# phase's assertions could not fail and found one guard with no coverage at all.
#
# Every mutation here corresponds to something the review demonstrated by
# running it, not to something it suspected. That is the difference between a
# mutation for a bug that was real and one for a bug that was imagined.
# ############################################################################

# FOUND-IN-REVIEW. The one guard with no test in either direction: every gap
# assertion used a script with no 400s in it. Without this pause the lane fires
# MAX_CONSECUTIVE_REJECTIONS requests back-to-back, parks the job, then -- since
# the tick still reports `worked` -- comes straight back for the next job and
# does it again. Under a parser-wide failure that is 3 x queue depth unpaced
# requests against an IP-based rate limit.
run_mutation "drop the pacing on the HTTP 400 retry path (unpaced burst)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """          await this.pause(signal);
          continue;"""
new = """          continue;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# FOUND-IN-REVIEW. The jitter test injected `random: () => 0`, pinning the draw
# to the floor -- it produced a metronome and asserted a metronome was in bounds.
# This is the mutation that was green before the test was rewritten.
run_mutation "replace the jitter with a fixed interval (a metronome is a fingerprint)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """    const ms = this.opts.sleepMsMin + Math.floor(this.random() * (span + 1));"""
new = """    const ms = this.opts.sleepMsMin + Math.floor(0 * span);"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# FOUND-IN-REVIEW. The guard originally rejected only a cursor EQUAL to the
# current one, leaving the worse shape open: a cursor that has gone UP walks back
# towards the newest video and page one comes round again.
run_mutation "narrow the cursor guard back to equality only (backwards cursor loops)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (page.hasMore && cursor !== 0 && nextCursor >= cursor) {"""
new = """      if (page.hasMore && cursor !== 0 && nextCursor === cursor) {"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "hand a fractional cursor straight to a bigint column" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      const nextCursor = Math.trunc(page.maxCursor);"""
new = """      const nextCursor = page.maxCursor;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# FOUND-IN-REVIEW. `checkpoint` discarded a stale write but still answered
# `true`, telling an overtaken straggler to keep paging -- two concurrent
# request streams against an IP-rate-limited endpoint, which is a live F2
# violation produced by the code protecting the checkpoint.
run_mutation "report a DISCARDED checkpoint as still-ours (straggler keeps paging)" \
  "$JOBS_REPO" "test/db/jobs.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """        lt(jobs.enumeratedCount, input.enumeratedCount),
      ),
    )"""
new = """      ),
    )"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

run_mutation "checkpoint a page that added nothing (no-op page reads as not-ours)" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """      if (page.awemeList.length === 0) break;"""
new = """      if (false) break;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# FOUND-IN-REVIEW. The leak test asserted only the absence of cookie field names
# -- which this lane never holds, so no mutation of it could ever fail. It now
# asserts the sec_user_id, which the lane really does hold, and this is the
# mutation the review used to prove the old version unfalsifiable.
run_mutation "log the sec_user_id in the success line" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """        `; job is now downloading`,"""
new = """        `; job is now downloading https://www.douyin.com/user/${job.secUserId}`,"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# FOUND-IN-REVIEW. The dropped-count assertion was `toMatch(/2/)`, satisfied by
# any 2 anywhere in the log -- in practice by the job UUID, ~7 runs in 8. A
# FLAKY green, which is worse than a hard one.
run_mutation "stop reporting how many items were dropped as unusable" \
  "$LANE" "test/lanes/enumerate.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/enumerate.lane.ts")
s = p.read_text()
old = """        (dropped > 0 ? `, ${dropped} item(s) dropped as unusable` : \x27\x27) +"""
new = """        \x27\x27 +"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# FOUND-IN-REVIEW. BLOCK 3 proves the recovery test notices the STATUS WORD. It
# does not prove the second, behavioural mechanism the test claims -- that a
# blocked job is still cancellable because it is live-but-stuck. This aims at
# that mechanism alone, and only passes because the assertion was reordered to
# call `cancel` before asserting the word.
run_mutation "make a blocked job un-cancellable (the behavioural half of blocked != failed)" \
  "$JOBS_REPO" "test/lanes/cookie-recovery" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
old = """const CANCELLABLE_STATUSES = [\x27pending\x27, \x27enumerating\x27, \x27downloading\x27, \x27blocked\x27] as const;"""
new = """const CANCELLABLE_STATUSES = [\x27pending\x27, \x27enumerating\x27, \x27downloading\x27] as const;"""
assert old in s
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 12 -- 图文 IMAGE POSTS. Added 2026-08-26, after a bug that SHIPPED and
# ran to completion looking perfect.
#
# An image post has no video. Douyin still fills `video.play_addr.url_list` --
# with the BACKGROUND MUSIC CDN. `toVideoUpsert` never asked what kind of post it
# was, so the download lane fetched an MP3, wrote it to `<aweme_id>.mp4`, matched
# it against `content-length`, renamed it into place and marked the row `done`.
# A real 215-item profile reported 215/215 completed, 0 zero-byte files, 0
# leftover `.tmp`, and `file(1)` called two of the files "MPEG ADTS, layer III,
# 128 kbps". Every counter agreed with every other counter.
#
# It is the same family as phase 6's zero-byte commit and phase 7's
# skipped-under-a-live-job, and like both of those it had no mutation because it
# had no guard. The guard now records those items as `skipped` -- NOT dropped:
# dropping shrinks `total_videos` and the posts vanish with all the numbers still
# agreeing, which is the same silence wearing a different hat.
#
# The four mutations are the four ways to put the bug back, and they fail in BOTH
# directions on purpose. M4 skips EVERYTHING: over-skipping is worse than the
# original bug (it loses the whole archive) and it would be just as quiet, so the
# suite has to be able to see it.
# ############################################################################

run_mutation "treat an image post as a video again (the bug, exactly as it shipped)" \
  "$MAPPER" "test/lanes/image-post-skip" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """function isImagePost(aweme: Record<string, unknown>): boolean {
  const images = aweme.images;
  return Array.isArray(images) && images.length > 0;
}"""
new = """function isImagePost(aweme: Record<string, unknown>): boolean {
  void aweme;
  return false;
}"""
assert old in s, "isImagePost changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 4 / the control this block turns on. The mutation above going red says the
# line matters SOMEHOW. It does not say the red came from the image/video
# distinction rather than from the mapper breaking generally -- and a mapper that
# throws would turn every suite in the repo red and look like a caught mutation.
# So the SAME edit is measured against the three suites that predate image posts
# and are about videos: they must not move.
run_control "image posts treated as videos (the video path must not move)" \
  "$MAPPER" "test/lanes/aweme.mapper test/lanes/enumerate.lane test/db/videos.repository" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """function isImagePost(aweme: Record<string, unknown>): boolean {
  const images = aweme.images;
  return Array.isArray(images) && images.length > 0;
}"""
new = """function isImagePost(aweme: Record<string, unknown>): boolean {
  void aweme;
  return false;
}"""
assert old in s, "isImagePost changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "skip EVERY item (over-skipping loses the archive, just as quietly)" \
  "$MAPPER" "test/lanes/image-post-skip test/lanes/aweme.mapper" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """  return Array.isArray(images) && images.length > 0;"""
new = """  void images;
  return true;"""
assert old in s, "the images discriminator changed shape"
p.write_text(s.replace(old, new, 1))
'

# The discriminator is `images` BECAUSE `media_type`/`aweme_type` are enums on
# somebody elses API: 2/68 for images and 4/55 for video today, and Douyin adds
# values (live replays, collections, mixed posts). A guard that also demanded the
# enum would start downloading MP3s the day a new number appears -- and would do
# it with every counter agreeing, exactly like the original. This is that edit.
run_mutation "demand the media_type enum as well as the images array" \
  "$MAPPER" "test/lanes/image-post-skip" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """  return Array.isArray(images) && images.length > 0;"""
new = """  return Array.isArray(images) && images.length > 0 && aweme.media_type === 2;"""
assert old in s, "the images discriminator changed shape"
p.write_text(s.replace(old, new, 1))
'

# The mappers verdict is worth nothing if the write drops it. `upsertMany` is the
# only statement that ever sets these rows status, because `ON CONFLICT DO
# NOTHING` means there is exactly one moment in a rows life when the enumerate
# lane can say anything about it at all.
run_mutation "the insert ignores the rows status and queues everything as pending" \
  "$VIDEOS_REPO" "test/lanes/image-post-skip test/db/videos.repository" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """        status: row.status ?? \x27pending\x27,"""
new = """        status: \x27pending\x27,"""
assert old in s, "the upsert status carry changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 7 in its second half: a mutation that STAYS GREEN is the only channel that
# catches an assertion pinned to the wrong thing. The image-post suite asserts
# that a skipped row CARRIES a reason, never what the reason says -- because
# `markSkipped` leaves `last_error` null for a cancelled video and the difference
# between "no reason" and "a reason" is the invariant, while the prose is not. If
# rewording this string turns the suite red, the suite is measuring English.
run_control "reword the skip reason (the suite must measure the status, not the prose)" \
  "$MAPPER" "test/lanes/image-post-skip" \
  py '
import pathlib
p = pathlib.Path("src/lanes/aweme.mapper.ts")
s = p.read_text()
old = """const IMAGE_POST_REASON ="""
new = """const IMAGE_POST_REASON: string = \x27nothing here to download\x27;
const UNUSED_IMAGE_POST_REASON ="""
assert old in s, "IMAGE_POST_REASON changed shape"
p.write_text(s.replace(old, new, 1))
'

echo
if [ "$fail" -eq 0 ]; then
  echo "All phase-5 mutations applied; every mutation was caught and every control stayed green."
else
  echo "PHASE 5 MUTATION SUITE FAILED -- see above."
fi
exit "$fail"

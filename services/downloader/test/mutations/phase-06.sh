#!/usr/bin/env bash
#
# Phase 6 mutation suite (plan.md §5, phase-06 DoD).
#
# "The suite is green" is evidence the suite RAN, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# named tests to go red. All seven enforcement rules from plan §5 are carried
# over, because each exists because of a specific way a mutation harness has
# already lied in this repo:
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
#
# Phase 6's two DoD mutations are BLOCK 1 and BLOCK 2. Everything after them is
# a guard this phase added on its own, and BLOCK 3 is the one worth reading:
# it removes only the `attempts` half of the ownership check, leaving a check on
# the lease itself -- which is what `markDone`'s phase-1 comment claims is not
# protection at all. That claim now has a test.
#
# Run from services/downloader:  bash test/mutations/phase-06.sh
# Needs the docker-compose stack up (Postgres).

set -u

cd "$(dirname "$0")/../.." || exit 1

STORAGE=src/storage/local-disk.storage.ts
LANE=src/lanes/download.lane.ts
VIDEOS_REPO=src/db/videos.repository.ts
LANES_MODULE=src/lanes/lanes.module.ts

BACKUP_DIR=$(mktemp -d)
fail=0

# --- RULE 5 -------------------------------------------------------------------
PRISTINE_DIR=$(mktemp -d)
ALL_TARGETS="$STORAGE $LANE $VIDEOS_REPO $LANES_MODULE"
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

# The M1-gate e2e suite is the slowest thing any pattern here can select, at
# ~5s. 300s is generous by nearly two orders.
JEST_TIMEOUT_SECONDS=300

# 0 = green, 1 = red, 98 = never finished, 99 = the pattern selected nothing.
#
# `pnpm exec jest`, NOT `pnpm test -- ...`, and that is not a style choice.
# Everything after `--` reaches jest as a POSITIONAL argument, so a control that
# needs `-t CONTROL:` to select one test out of a file gets `-t` and `CONTROL:`
# folded into the path regex instead: jest then matches
# `/download-commit-race|-t|CONTROL:/`, runs the WHOLE file, and the control
# reports red because the must-fail test in the same file is failing exactly as
# intended. Measured, not theorised -- it is how this suite first ran, and both
# rule-4 controls reported a broken invariant that was not broken.

# Kills a process and everything under it. `$!` is the pnpm WRAPPER, so killing
# it alone orphans the node process and jest's workers -- which keep their
# Postgres connections and their schemas, so rule 5's "a killed harness leaves
# no poison" holds for the source tree but not for the DATABASE the next
# mutation is measured against.
kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill -9 "$pid" 2>/dev/null
}

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
  # RULE 2, in the shape a NAME pattern gives it, and the first version of this
  # line got it wrong in a way worth recording. It looked for "0 total" -- but a
  # `-t` that matches nothing does not print that: it prints
  #
  #     Test Suites: 1 skipped, 0 of 1 total
  #     Tests:       7 skipped, 7 total
  #
  # with exit code 0. Neither "No tests found" nor "0 total" matches, so the
  # control would report "still green, as required" having EXECUTED NOTHING --
  # and the two controls are what decide which mechanism carries each of this
  # phase's two DoD mutations. So the requirement is positive evidence that at
  # least one test actually ran, not the absence of a particular phrase.
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
# BLOCK 1 -- DoD: "bỏ kiểm lease trước `rename` -> test 2-writer phải ĐỎ".
#
# The reaper hands a video whose lease expired to another worker (F3), and an
# expired lease does not mean the old worker is dead. Both then hold the same
# key, the temp names are unique so their bytes cannot mix, and THE LAST RENAME
# WINS. The check immediately before the rename is what makes the stale worker
# decline instead.
#
# Applied at BOTH ends, because either edit alone would ship the bug:
#   1a  storage stops acting on the hook's answer
#   1b  the lane stops answering honestly
# ############################################################################

run_mutation "storage ignores beforeCommit's answer (1a: the check itself)" \
  "$STORAGE" "test/lanes/download-commit-race" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
old = """      if (beforeCommit && !(await beforeCommit())) {
        await unlink(tmpPath).catch(() => undefined);
        return null;
      }"""
new = """      if (beforeCommit) await beforeCommit();"""
assert old in s, "the beforeCommit decline block changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "the lane always says it still owns the claim (1b: the answer)" \
  "$LANE" "test/lanes/download-commit-race" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = """            if (!(await videosRepository.holdsClaim(this.db, video.awemeId, token))) {
              lost = true;
              return false;
            }
            return true;"""
new = """            return true;"""
assert old in s, "the beforeCommit hook changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 4. Two mechanisms guard this key: the UNIQUE TEMP NAME (phase 2) stops
# the two workers bytes from MIXING, and the ownership check decides WHOSE
# complete file wins. Without this control, "the mutation went red" would only
# say the line matters somehow -- not which of the two it was standing in for.
run_control "storage ignores beforeCommit (mixture control must stay green)" \
  "$STORAGE" "test/lanes/download-commit-race -t CONTROL:" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
old = """      if (beforeCommit && !(await beforeCommit())) {
        await unlink(tmpPath).catch(() => undefined);
        return null;
      }"""
new = """      if (beforeCommit) await beforeCommit();"""
assert old in s, "the beforeCommit decline block changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 2 -- DoD: "bỏ verify content-length -> test file cụt phải ĐỎ".
#
# A body that stops early is otherwise indistinguishable from a complete one:
# the file exists, has a plausible size, and the row says `done`. Nobody finds
# out until somebody tries to play it.
#
# The condition is made unsatisfiable rather than deleted, so `expectedBytes`
# stays referenced and the mutation cannot go red merely by failing to compile
# -- a red for the wrong reason is not evidence.
# ############################################################################

run_mutation "drop the advertised-length verification" \
  "$STORAGE" "test/lanes/download-commit-race" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
old = "      if (expectedBytes !== undefined && bytes !== expectedBytes) {"
new = "      if (expectedBytes !== undefined && bytes !== expectedBytes && expectedBytes < 0) {"
assert old in s, "the expectedBytes check changed shape"
p.write_text(s.replace(old, new, 1))
'

# RULE 4 again, and this is the pairing that makes BLOCK 2 mean something. A
# real dropped connection is caught by the TRANSPORT, not by the byte count, so
# a suite that only had that case would go green with the check deleted. The
# control names which test is which.
run_control "drop the length verification (transport control must stay green)" \
  "$STORAGE" "test/lanes/download-commit-race -t CONTROL:" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
old = "      if (expectedBytes !== undefined && bytes !== expectedBytes) {"
new = "      if (expectedBytes !== undefined && bytes !== expectedBytes && expectedBytes < 0) {"
assert old in s, "the expectedBytes check changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 3 -- the claim token, which is the part that is easy to get wrong.
#
# `markDone`s phase-1 comment says a bare lease check "would look like
# protection while providing none, since a lease being live says nothing about
# whose lease it is". This mutation removes ONLY the `attempts` comparison,
# leaving `status = downloading AND lease_expires_at > now()` -- i.e. exactly
# the plausible-looking check that comment warns about. If the 2-writer test
# stays green here, then that comment is decoration and the guard is the lease
# after all. It must go red.
# ############################################################################

run_mutation "ownership check keeps the lease but drops the claim token" \
  "$VIDEOS_REPO" "test/lanes/download-commit-race test/db/download-claim" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """  return and(
    eq(videos.awemeId, awemeId),
    eq(videos.status, \x27downloading\x27),
    eq(videos.attempts, attempts),
    gt(videos.leaseExpiresAt, sql`now()`),
  );"""
new = """  return and(
    eq(videos.awemeId, awemeId),
    eq(videos.status, \x27downloading\x27),
    gt(videos.leaseExpiresAt, sql`now()`),
  );"""
assert old in s, "ownsClaim changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 4 -- F1, and the risk register line "Ai đó sửa cho đúng IDEA.md §3.2".
#
# IDEA.md §3.2 asks for a re-resolve before every download. Doing that costs
# ~525 requests for a 500-video profile against ~25, which is 3-5x the hourly
# budget against an IP-based rate limit. So the cache is a DELIBERATE deviation
# (plan §7), and "somebody makes it match the spec" is the most likely way it
# gets undone. This is that edit.
# ############################################################################

run_mutation "re-resolve every video, as IDEA.md §3.2 asks (F1 undone)" \
  "$LANE" "test/lanes/download.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = """    const cached =
      video.playAddr !== null && (await videosRepository.isUrlFresh(this.db, video.awemeId))
        ? video.playAddr
        : null;"""
new = """    const cached = null;"""
assert old in s, "the F1 cache read changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "treat a 403/410 as a plain failure instead of re-resolving" \
  "$LANE" "test/lanes/download.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = "      if (REFUSALS.has(res.status)) {"
new = "      if (REFUSALS.has(res.status) && res.status < 0) {"
assert old in s, "the refusal branch changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# BLOCK 5 -- the rest of this phase's guards.
# ############################################################################

run_mutation "stop asking whether the job was cancelled before committing" \
  "$LANE" "test/lanes/download-commit-race" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = """            if (await jobsRepository.isProfileCancelled(this.db, video.secUserId)) {
              cancelled = true;
              return false;
            }
            if (!(await videosRepository.holdsClaim("""
new = """            if (!(await videosRepository.holdsClaim("""
assert old in s, "the cancel branch of the commit hook changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "renew the lease on a timer instead of on progress" \
  "$LANE" "test/lanes/download-commit-race" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = """      const seen = transferred();
      if (seen === lastSeen) return;
      lastSeen = seen;"""
new = """      const seen = transferred();
      lastSeen = seen;"""
assert old in s, "the heartbeat progress gate changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "finalize every profile, not the one this lane just finished" \
  "$LANE" "test/lanes/download.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = "    const finalized = await jobsRepository.finalizeSettled(this.db, { secUserId });"
new = "    const finalized = await jobsRepository.finalizeSettled(this.db);"
assert old in s, "the finalize call changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "register ONE download lane instead of DOWNLOAD_CONCURRENCY" \
  "$LANES_MODULE" "test/lanes/lanes.module" \
  py '
import pathlib
p = pathlib.Path("src/lanes/lanes.module.ts")
s = p.read_text()
old = "          { length: env.DOWNLOAD_CONCURRENCY },"
new = "          { length: 1 },"
assert old in s, "the download-lane factory changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "leak the temp file when a commit is declined" \
  "$STORAGE" "test/storage/before-commit test/lanes/download-commit-race" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
old = """        await unlink(tmpPath).catch(() => undefined);
        return null;"""
new = """        return null;"""
assert old in s, "the declined-commit cleanup changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "give a claim back unconditionally, ignoring the claim token" \
  "$VIDEOS_REPO" "test/db/download-claim" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
old = """  return opts.attempts === undefined
    ? eq(videos.awemeId, awemeId)
    : ownsClaim(awemeId, opts.attempts);"""
new = """  void opts;
  return eq(videos.awemeId, awemeId);"""
assert old in s, "giveBackTarget changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "keep streaming after the claim is lost (drop the heartbeat abort)" \
  "$LANE" "test/lanes/download-commit-race" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = """          on.onLost();
        }"""
new = """        }"""
assert old in s, "the heartbeat lost-claim branch changed shape"
p.write_text(s.replace(old, new, 1))
'

run_mutation "commit a body with no bytes in it" \
  "$LANE" "test/lanes/download.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = """            if (transferred === 0) {
              empty = true;
              return false;
            }"""
new = """            if (transferred < 0) {
              empty = true;
              return false;
            }"""
assert old in s, "the zero-byte guard changed shape"
p.write_text(s.replace(old, new, 1))
'

# ############################################################################
# CONTROL -- the harness itself must be able to report GREEN.
#
# Every "caught" above is only meaningful if this run can also produce a pass.
# A harness whose every answer is "red" is indistinguishable from a broken
# build, and this repo has shipped that shape twice (plan §5, rules 2 and 3).
# ############################################################################

run_control "reword a log line (must change nothing observable)" \
  "$LANE" "test/lanes/download.lane" \
  py '
import pathlib
p = pathlib.Path("src/lanes/download.lane.ts")
s = p.read_text()
old = "`lane ${this.name}: video ${video.awemeId} downloaded (${committed.bytes} bytes)`"
new = "`lane ${this.name}: video ${video.awemeId} FETCHED (${committed.bytes} bytes)`"
assert old in s, "the success log line changed shape"
p.write_text(s.replace(old, new, 1))
'

echo
echo "=============================================================="
if [ "$fail" -eq 0 ]; then
  echo "PHASE 6 MUTATIONS: every must-fail was caught, every control stayed green."
else
  echo "PHASE 6 MUTATIONS: FAILURES ABOVE. A mutation that stayed green is a guard"
  echo "nobody is watching; a control that went red means the invariant is resting"
  echo "on a different mechanism than its comment claims. Read plan.md §5."
fi
echo "=============================================================="
exit "$fail"

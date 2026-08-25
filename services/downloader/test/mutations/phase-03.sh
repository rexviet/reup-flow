#!/usr/bin/env bash
#
# Phase 3 mutation suite (plan.md §5, phase-03 DoD).
#
# "The suite is green" is evidence the suite ran, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# named tests to go red. The three enforcement rules, all learned the hard way
# in earlier phases:
#
#   1. Every mutation prints the diff it actually applied. A sed that matches
#      nothing is a mutation that never ran -- reporting that as "passed" is
#      how a guard silently dies. An empty diff is a hard error, not a skip.
#      (Phase 1 sprang this trap twice.)
#   2. Every mutation restores its file, and the restore is verified.
#   3. When an invariant is held by MORE THAN ONE mechanism, a mutation going
#      red proves only that the line it touched matters somehow -- not which
#      mechanism carries the invariant. So each such invariant also gets a
#      CONTROL that must stay GREEN. (Phase 2's lesson, `run_control`.)
#
# Phase 3's headline invariant -- "a cookie never reaches a log, an error, or
# a response" (plan §0.3) -- is held by TWO overlapping mechanisms (the logger
# wrapper and the process-wide stdio patch) each backed by TWO redaction
# mechanisms (field pattern and exact-value registry). That is four things
# that could each be silently load-bearing or silently dead, which is why the
# cookie block below is the longest.
#
# Run from services/downloader:  bash test/mutations/phase-03.sh
# Needs the docker-compose stack up (Postgres + Redis).

set -u

cd "$(dirname "$0")/../.." || exit 1

REDACTOR=src/redaction/redactor.ts
STDIO=src/redaction/stdio-redaction.ts
LOGGER=src/logging/redact.logger.ts
BOOTSTRAP=src/bootstrap.ts
GUARD=src/auth/api-key.guard.ts
COOKIE_SVC=src/cookie/cookie.service.ts
JOBS_SVC=src/jobs/jobs.service.ts
JOBS_MAPPER=src/jobs/jobs.mapper.ts
JOBS_REPO=src/db/jobs.repository.ts
VIDEOS_REPO=src/db/videos.repository.ts

BACKUP_DIR=$(mktemp -d)
trap 'rm -rf "$BACKUP_DIR"' EXIT
fail=0

# Backup/restore operate on a LIST of files, because some mutations here have
# to break two files at once (see the combined leak mutation).
#
# An earlier revision of this script restored the second file with
# `git checkout --`, which silently does NOTHING for a file git does not yet
# track -- and every phase-3 source file is new. The stdio patch stayed
# mutated for the rest of the run, every later cookie assertion was measuring
# a crippled build, and the suite still reported those mutations as "caught".
# Hence: one restore mechanism, applied to every file, and VERIFIED. A
# mutation harness that cannot prove it put the code back is a harness that
# quietly poisons everything after it.
backup() { for f in $1; do cp "$f" "$BACKUP_DIR/$(basename "$f").bak"; done; }
restore() {
  for f in $1; do
    cp "$BACKUP_DIR/$(basename "$f").bak" "$f"
    diff -q "$BACKUP_DIR/$(basename "$f").bak" "$f" >/dev/null \
      || { echo "FATAL: failed to restore $f"; exit 1; }
  done
}

# _apply <label> <files> <mutator...> -- applies and refuses an empty diff.
# <files> is one space-separated argument; the diff check requires that AT
# LEAST ONE listed file actually changed.
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
  # EVERY listed file must have changed, not merely one of them. A file is
  # listed here precisely because the mutation is supposed to touch it, so a
  # file that came through untouched means the mutation half-applied. Accepting
  # that ("at least one file changed") is a subtler version of the empty-diff
  # trap and it bit this very suite: the combined leak mutation below silently
  # applied only its stdio half for one revision, and reported itself caught.
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

# run_mutation <label> <file> <test-pattern> <mutator...> -- tests MUST go red.
run_mutation() {
  local label=$1 files=$2 pattern=$3
  shift 3
  _apply "$label" "$files" "$@" || return
  echo "  tests: $pattern (must FAIL)"
  run_jest "$pattern"
  local rc=$?
  if [ "$rc" -eq 99 ]; then
    report_no_tests "$pattern"
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

# run_control <label> <file> <test-pattern> <mutator...> -- tests MUST stay green.
run_control() {
  local label=$1 files=$2 pattern=$3
  shift 3
  _apply "CONTROL: $label" "$files" "$@" || return
  echo "  tests: $pattern (must STAY GREEN)"
  run_jest "$pattern"
  local rc=$?
  if [ "$rc" -eq 99 ]; then
    report_no_tests "$pattern"
  elif [ "$rc" -eq 0 ]; then
    echo "OK: still green, as required."
  else
    echo "FAIL: control went red -- the invariant is resting on the wrong mechanism."
    grep -E '✕' "$BACKUP_DIR/out.log" || tail -20 "$BACKUP_DIR/out.log"
    fail=1
  fi
  restore "$files"
}

# run_lint_mutation <label> <file> <mutator...> -- `pnpm lint` MUST go red.
# Layer 1 of the §3.4 boundary enforcement is ESLint, which fails at edit
# time rather than at test time; asserting it needs the linter, not jest.
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

# Runs jest with <patterns> word-split into SEPARATE arguments.
#
# `pnpm test -- "$pattern"` passes a multi-word pattern as ONE regex, which
# matches nothing and exits 1. That is indistinguishable, by exit code alone,
# from a mutation being caught -- so five mutations in this file silently ran
# no tests at all and reported themselves "caught", while two controls reported
# themselves "red". Both directions were wrong and both looked plausible.
#
# So: split the patterns, and treat "no tests found" as a HARD ERROR rather
# than as a signal in either direction. This is the same rule as the empty-diff
# check, applied to the other half of a mutation -- a mutation is only evidence
# if the code changed AND tests actually ran.
# Returns 0 = green, 1 = red, 99 = the pattern selected nothing.
run_jest() {
  # shellcheck disable=SC2086 -- word-splitting is the point.
  pnpm test -- $1 >"$BACKUP_DIR/out.log" 2>&1
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

py() { python3 -c "$1"; }

# ############################################################################
# BLOCK 1 -- plan §0.3 / §5: "cookie never reaches a log". The DoD names two
# mutations here and insists they are SEPARATE, because phase 1's leak was a
# `print()` that a logger-watching test never saw.
# ############################################################################

# --- 1a. The logger path, on its own. ---------------------------------------
# test/logging/redact.logger.spec.ts deliberately runs with the stdio patch
# NOT installed, so it isolates this mechanism from the one below.
run_mutation "RedactingLogger.log stops redacting (the 'logger.log(cookie)' path)" \
  "$LOGGER" "test/logging/redact.logger" \
  py '
import pathlib
p = pathlib.Path("src/logging/redact.logger.ts")
s = p.read_text()
s = s.replace(
  "    this.inner.log(redactValue(message), ...(optionalParams.map(redactValue) as [string?]));",
  "    this.inner.log(message as string, ...(optionalParams as [string?]));")
p.write_text(s)
'

# --- 1b. The raw stdout/stderr path, on its own. ----------------------------
# SEPARATE from 1a by design -- this is the exact gap that let phase 1's
# `print()` through. It must be caught by BOTH the unit test and the
# real-child-process leak test.
#
# Anchor updated for FIX 6 (review): stdio-redaction.ts now line-buffers
# per stream (a per-call-only redaction misses a secret split across two
# write() calls -- see LineBuffer's comment), so the single-line
# `redactor.redact(chunk)` this mutation used to target no longer exists.
# The line-terminated write path -- what `console.log(cookie)` actually
# takes, since console.log always appends a trailing newline -- is
# `LineBuffer.append`'s normal (non-overflow) branch.
run_mutation "the stdio patch stops redacting strings (the 'console.log(cookie)' path)" \
  "$STDIO" "test/redaction/" \
  py '
import pathlib
p = pathlib.Path("src/redaction/stdio-redaction.ts")
s = p.read_text()
s = s.replace(
  "    const toFlush = combined.slice(0, lastNewline + 1);\n    this.pending = combined.slice(lastNewline + 1);\n    return { shouldForward: true, text: redactor.redact(toFlush) };",
  "    const toFlush = combined.slice(0, lastNewline + 1);\n    this.pending = combined.slice(lastNewline + 1);\n    return { shouldForward: true, text: toFlush };")
p.write_text(s)
'

# --- 1c/1d. The two redaction MECHANISMS, isolated from each other. ---------
# The cookie-leak harness emits one canary only the pattern can scrub and one
# only the registry can scrub, which is what lets these two mutations be told
# apart at all.
run_mutation "drop the field=value pattern, keep only the exact-value registry" \
  "$REDACTOR" "test/redaction/" \
  py '
import pathlib, re
p = pathlib.Path("src/redaction/redactor.ts")
s = p.read_text()
# Matched by regex rather than by an exact literal: the replacement callback
# grew capture groups (so redaction preserves JSON delimiters) and would
# otherwise silently stop matching -- which the empty-diff guard catches, but
# only after somebody has to work out why.
s = re.sub(r"let result = text\.replace\(\n.*?\n    \);", "let result = text;", s, flags=re.S)
p.write_text(s)
'

run_mutation "drop the exact-value registry, keep only the field=value pattern" \
  "$REDACTOR" "test/redaction/" \
  py '
import pathlib
p = pathlib.Path("src/redaction/redactor.ts")
s = p.read_text()
s = s.replace("    for (const secret of this.secrets) {", "    for (const secret of [] as string[]) {")
p.write_text(s)
'

# --- 1e. The registration call site. ----------------------------------------
# The registry only knows a cookie because CookieService.push told it. Without
# that call the registry is present, correct, and empty.
run_mutation "CookieService.push stops registering the cookie with the redactor" \
  "$COOKIE_SVC" "test/cookie/ test/redaction/cookie-leak" \
  py '
import pathlib
p = pathlib.Path("src/cookie/cookie.service.ts")
s = p.read_text()
s = s.replace("    redactor.registerSecret(cookie);\n", "")
p.write_text(s)
'

# --- 1f. The same, for the DSN at bootstrap (D-3). --------------------------
run_mutation "bootstrap stops registering DATABASE_URL as a secret (D-3)" \
  "$BOOTSTRAP" "test/bootstrap-failure" \
  py '
import pathlib
p = pathlib.Path("src/bootstrap.ts")
s = p.read_text()
s = s.replace("  redactor.registerSecret(env.DATABASE_URL);\n", "")
p.write_text(s)
'

# --- 1g/1h. CONTROLS: a careless call site must be HARMLESS. ----------------
# These inject the leak AFTER redactor.registerSecret(cookie), which is where
# that call now sits. It did not always: it used to be the last statement of
# push(), leaving the whole method body as a window where the cookie was live
# and not yet redactable. THESE CONTROLS ARE WHAT FOUND THAT -- they went red,
# on a correct-looking implementation with a fully green suite. See the long
# comment on the registerSecret call in cookie.service.ts.
# plan §0.3's actual claim is "redact at the LAYER, not the call site". These
# inject exactly the leak the DoD names -- logging the cookie -- and require
# the suite to stay GREEN. If either goes red, the layer is not doing its job
# and the codebase is relying on nobody ever writing this line.
run_control "inject console.log(cookie) into CookieService.push" \
  "$COOKIE_SVC" "test/cookie/ test/redaction/cookie-leak" \
  py '
import pathlib
p = pathlib.Path("src/cookie/cookie.service.ts")
s = p.read_text()
s = s.replace(
  "    redactor.registerSecret(cookie);\n\n    const results = await this.redis",
  "    redactor.registerSecret(cookie);\n    console.log(`careless call site: ${cookie}`);\n\n    const results = await this.redis")
p.write_text(s)
'

run_control "inject a raw process.stderr.write(cookie) into CookieService.push" \
  "$COOKIE_SVC" "test/cookie/ test/redaction/cookie-leak" \
  py '
import pathlib
p = pathlib.Path("src/cookie/cookie.service.ts")
s = p.read_text()
s = s.replace(
  "    redactor.registerSecret(cookie);\n\n    const results = await this.redis",
  "    redactor.registerSecret(cookie);\n    process.stderr.write(`careless call site: ${cookie}\\n`);\n\n    const results = await this.redis")
p.write_text(s)
'

# --- 1i. The combined mutation the DoD is really asking for. ----------------
# 1b proves the stdio patch is load-bearing. 1g proves a careless
# console.log(cookie) is harmless while the patch is intact. THIS one removes
# the patch AND injects the leak together, and is the only one of the three
# that proves the TEST ITSELF WATCHES STDOUT rather than only the logger --
# which is precisely the false green phase 1 shipped.
run_mutation "inject console.log(cookie) AND no-op the stdio patch (the phase-1 false green)" \
  "$COOKIE_SVC $STDIO" "test/redaction/cookie-leak" \
  py '
import pathlib
svc = pathlib.Path("src/cookie/cookie.service.ts")
s = svc.read_text()
s = s.replace(
  "    redactor.registerSecret(cookie);\n\n    const results = await this.redis",
  "    redactor.registerSecret(cookie);\n    console.log(`careless call site: ${cookie}`);\n\n    const results = await this.redis")
svc.write_text(s)
# Anchor updated for FIX 6 (review) -- see the matching comment on mutation
# 1b above: the single-line `redactor.redact(chunk)` this used to target no
# longer exists after stdio-redaction.ts switched to per-stream line
# buffering. `console.log(...)` always appends a trailing newline, so the
# line-terminated (non-overflow) branch of `LineBuffer.append` is the one
# that must stop redacting to reproduce "the stdio patch does nothing".
stdio = pathlib.Path("src/redaction/stdio-redaction.ts")
t = stdio.read_text()
t = t.replace(
  "    const toFlush = combined.slice(0, lastNewline + 1);\n    this.pending = combined.slice(lastNewline + 1);\n    return { shouldForward: true, text: redactor.redact(toFlush) };",
  "    const toFlush = combined.slice(0, lastNewline + 1);\n    this.pending = combined.slice(lastNewline + 1);\n    return { shouldForward: true, text: toFlush };")
stdio.write_text(t)
'
# ############################################################################
# BLOCK 2 -- phase-03 §3.2 / Risks: "status must not infer age from TTL", and
# "status must never return the cookie value".
# ############################################################################

run_mutation "derive age_seconds from the TTL instead of pushed_at" \
  "$COOKIE_SVC" "test/cookie/" \
  py '
import pathlib
p = pathlib.Path("src/cookie/cookie.service.ts")
s = p.read_text()
s = s.replace(
  "      this.redis.get(cookiePushedAtKey(platform)),",
  "      this.redis.ttl(cookieCurrentKey(platform)).then((ttl) => ttl < 0 ? null : String(Date.now() - (COOKIE_TTL_SECONDS - ttl) * 1000)),")
p.write_text(s)
'

run_mutation "stop writing pushed_at, so age is unknowable" \
  "$COOKIE_SVC" "test/cookie/" \
  py '
import pathlib
p = pathlib.Path("src/cookie/cookie.service.ts")
s = p.read_text()
s = s.replace(
  "      .set(cookiePushedAtKey(platform), String(Date.now()), \x27EX\x27, COOKIE_TTL_SECONDS)\n", "")
p.write_text(s)
'

run_mutation "leak the cookie value through an existing status field" \
  "$COOKIE_SVC" "test/cookie/ test/redaction/cookie-leak" \
  py '
import pathlib
p = pathlib.Path("src/cookie/cookie.service.ts")
s = p.read_text()
s = s.replace(
  "      last_ok_at: lastOkAt,",
  "      last_ok_at: await this.redis.get(cookieCurrentKey(platform)),")
p.write_text(s)
'

# ############################################################################
# BLOCK 3 -- phase-03 DoD: "missing/wrong X-Api-Key -> 401 on EVERY route,
# including /internal".
# ############################################################################

run_mutation "guard returns false instead of throwing 401 (yields 403)" \
  "$GUARD" "test/auth/" \
  py '
import pathlib
p = pathlib.Path("src/auth/api-key.guard.ts")
s = p.read_text()
s = s.replace("      throw new UnauthorizedException();", "      return false;")
p.write_text(s)
'

# The specific shortcut this DoD item exists to forbid: treating /internal as
# safe because "only the extension calls it".
run_mutation "guard waves through anything under /internal" \
  "$GUARD" "test/auth/ test/cookie/" \
  py '
import pathlib
p = pathlib.Path("src/auth/api-key.guard.ts")
s = p.read_text()
s = s.replace(
  "    if (isPublic) return true;\n",
  "    if (isPublic) return true;\n    const probe = context.switchToHttp().getRequest<{ url?: string }>();\n    if (typeof probe.url === \x27string\x27 && probe.url.startsWith(\x27/internal\x27)) return true;\n")
p.write_text(s)
'

# ############################################################################
# BLOCK 4 -- phase-03 §3.1 + F6: "/retry resets attempts=0".
# ############################################################################

# Without the reset the row reads `pending` in every listing and progress
# count, yet claimOne's `attempts < maxAttempts` filter makes it invisible to
# the only query that ever picks up pending work. It sits there forever.
run_mutation "/retry flips status back to pending WITHOUT zeroing attempts (F6)" \
  "$VIDEOS_REPO" "test/db/videos.repository test/jobs/" \
  py '
import pathlib
p = pathlib.Path("src/db/videos.repository.ts")
s = p.read_text()
s = s.replace(
  ".set({ status: \x27pending\x27, attempts: 0, lastError: null, leaseExpiresAt: null })",
  ".set({ status: \x27pending\x27, lastError: null, leaseExpiresAt: null })")
p.write_text(s)
'

run_mutation "/retry leaves the job status alone (blocked stays blocked)" \
  "$JOBS_REPO" "test/db/jobs.repository test/jobs/" \
  py '
import pathlib
p = pathlib.Path("src/db/jobs.repository.ts")
s = p.read_text()
s = s.replace(
  ".set({ status: \x27pending\x27, error: null, leaseExpiresAt: null, updatedAt: sql`now()` })",
  ".set({ error: null, leaseExpiresAt: null, updatedAt: sql`now()` })")
p.write_text(s)
'

# ############################################################################
# BLOCK 5 -- phase-03 §3.1: POST /api/jobs does exactly three things.
# ############################################################################

run_mutation "skip the profile upsert, so the job insert hits a FK violation" \
  "$JOBS_SVC" "test/jobs/" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.service.ts")
s = p.read_text()
s = s.replace("      await jobsRepository.upsertProfile(tx, secUserId);\n", "")
p.write_text(s)
'

run_mutation "spread the ORM row into the response, leaking lease_expires_at" \
  "$JOBS_MAPPER" "test/jobs/" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.mapper.ts")
s = p.read_text()
s = s.replace(
  "  return {\n    job_id: job.id,",
  "  return {\n    ...(job as unknown as Record<string, unknown>),\n    job_id: job.id,")
p.write_text(s)
'

# ############################################################################
# BLOCK 6 -- plan §0 principle 1 / §3.4: the HTTP layer never calls a lane.
# Two enforcement layers, and the phase file says to keep BOTH. So both get
# their own mutation, each shaped to slip past the other layer.
# ############################################################################

# Layer 1: ESLint, at edit time. A plain static import.
run_lint_mutation "jobs.service takes a STATIC import on a lane (ESLint layer)" \
  "$JOBS_SVC" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.service.ts")
s = p.read_text()
p.write_text("import \x27../lanes/enumerate.lane\x27;\n" + s)
'

# Layer 2: the architecture test, at test time. A DYNAMIC import -- the exact
# bypass §3.4 says ESLint cannot see, and the whole reason the second layer
# exists rather than being "redundant with the linter".
run_mutation "jobs.service takes a DYNAMIC import on a lane (architecture-test layer)" \
  "$JOBS_SVC" "test/architecture/" \
  py '
import pathlib
p = pathlib.Path("src/jobs/jobs.service.ts")
s = p.read_text()
s += "\nasync function __loadLane(): Promise<unknown> {\n  return import(\x27../lanes/enumerate.lane\x27);\n}\nvoid __loadLane;\n"
p.write_text(s)
'

echo
if [ "$fail" -eq 0 ]; then
  echo "All phase-3 mutations applied; every mutation was caught and every control stayed green."
else
  echo "PHASE 3 MUTATION SUITE FAILED -- see above."
fi
exit "$fail"

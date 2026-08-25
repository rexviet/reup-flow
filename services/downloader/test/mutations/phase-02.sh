#!/usr/bin/env bash
#
# Phase 2 mutation suite (plan.md §5, phase-02 DoD).
#
# "The suite is green" is evidence the suite ran, not evidence the invariant
# holds. Each mutation below breaks one invariant on purpose and REQUIRES the
# named tests to go red. Same two rules as phase 1, both learned here:
#
#   1. Every mutation prints the diff it actually applied. A sed that matches
#      nothing is a mutation that never ran -- reporting that as "passed" is
#      how a guard silently dies. An empty diff is a hard error, not a skip.
#   2. Every mutation restores its file, and the restore is verified.
#
# There is also one CONTROL that must stay GREEN (see `run_control`): without
# it, a mutation suite can only tell you that SOMETHING is load-bearing, not
# WHICH thing.
#
# Run from services/downloader:  bash test/mutations/phase-02.sh
# Needs no docker: every suite below is in-process (a local http server and a
# temp directory).

set -u

cd "$(dirname "$0")/../.." || exit 1

STORAGE=src/storage/local-disk.storage.ts
CLIENT=src/parser/parser.client.ts

STORAGE_TESTS=test/storage/local-disk.storage
CLIENT_TESTS=test/parser/parser.client

BACKUP_DIR=$(mktemp -d)
trap 'rm -rf "$BACKUP_DIR"' EXIT
fail=0

backup() { cp "$1" "$BACKUP_DIR/$(basename "$1").bak"; }
restore() {
  cp "$BACKUP_DIR/$(basename "$1").bak" "$1"
  diff -q "$BACKUP_DIR/$(basename "$1").bak" "$1" >/dev/null \
    || { echo "FATAL: failed to restore $1"; exit 1; }
}

# _apply <label> <file> <mutator...> -- applies and refuses an empty diff.
_apply() {
  local label=$1 file=$2
  shift 2
  echo
  echo "=============================================================="
  echo "MUTATION: $label"
  echo "  file:  $file"
  echo "=============================================================="
  backup "$file"
  "$@"
  if diff -q "$BACKUP_DIR/$(basename "$file").bak" "$file" >/dev/null; then
    echo "ERROR: mutation matched nothing -- the code shape changed and this"
    echo "       mutation no longer applies. Update it; do NOT treat this as a pass."
    fail=1
    restore "$file"
    return 1
  fi
  echo "--- diff actually applied ---"
  diff "$BACKUP_DIR/$(basename "$file").bak" "$file"
  echo "-----------------------------"
  return 0
}

# run_mutation <label> <file> <test-pattern> <mutator...> -- tests MUST go red.
run_mutation() {
  local label=$1 file=$2 pattern=$3
  shift 3
  _apply "$label" "$file" "$@" || return
  echo "  tests: $pattern (must FAIL)"
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

# run_control <label> <file> <test-pattern> <mutator...> -- tests MUST stay green.
#
# A mutation going red proves only that the line it touched matters somehow.
# When two mechanisms sit on the same line -- a unique temp name AND an
# exclusive-create flag -- a control that removes the WEAKER one and stays
# green is what shows which mechanism is actually carrying the invariant.
run_control() {
  local label=$1 file=$2 pattern=$3
  shift 3
  _apply "CONTROL: $label" "$file" "$@" || return
  echo "  tests: $pattern (must STAY GREEN)"
  if pnpm test -- "$pattern" >"$BACKUP_DIR/out.log" 2>&1; then
    echo "OK: still green, as required."
  else
    echo "FAIL: control went red -- the invariant is resting on the wrong mechanism."
    grep -E '✕' "$BACKUP_DIR/out.log" || tail -20 "$BACKUP_DIR/out.log"
    fail=1
  fi
  restore "$file"
}

py() { python3 -c "$1"; }

# ============================================================================
# phase-02 DoD: "bỏ .tmp+rename, ghi thẳng file đích → test crash-giữa-ghi đỏ"
# ============================================================================
# Writing straight to the destination makes a half-finished download visible
# under its final name. `exists()` then reports a truncated file as present
# and phase 6 never re-fetches it.
run_mutation "write straight to the destination, no .tmp + rename" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("const tmpPath = `${path}.${randomUUID()}.tmp`;", "const tmpPath = path;")
p.write_text(s)
'

# ============================================================================
# phase-02 DoD: "đổi .tmp thành tên cố định → test 2 writer đồng thời đỏ"
# ============================================================================
# The reaper (F3) can hand a video to a new worker while the old one is still
# streaming. A per-key temp name lets both write the same file and commit an
# mp4 that is neither download.
run_mutation "derive the temp name from the key, so two writers share it" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("const tmpPath = `${path}.${randomUUID()}.tmp`;", "const tmpPath = `${path}.tmp`;")
s = s.replace("createWriteStream(tmpPath, { flags: \x27wx\x27 })", "createWriteStream(tmpPath, { flags: \x27w\x27 })")
p.write_text(s)
'

# The same mutation again, but with the LOSER'S RENAME MADE FORGIVING -- the
# shape phase 6 introduces when it declines to commit on an expired lease.
#
# This case exists because the mutation above was, for one revision, caught
# for the wrong reason: the second writer's `rename` hit ENOENT (the winner
# had already moved the shared temp file away) and the test died before it
# ever evaluated its corruption assertion. Tolerate that ENOENT and the test
# went flaky-green with both writers genuinely sharing one file. A guard that
# depends on an incidental crash is a guard that disappears the moment the
# crash is handled.
run_mutation "share the temp name AND tolerate the loser's ENOENT (phase-6 shape)" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("const tmpPath = `${path}.${randomUUID()}.tmp`;", "const tmpPath = `${path}.tmp`;")
s = s.replace("createWriteStream(tmpPath, { flags: \x27wx\x27 })", "createWriteStream(tmpPath, { flags: \x27w\x27 })")
s = s.replace("      const flush = await open(tmpPath, \x27r\x27);", "      const flush = await open(tmpPath, \x27r\x27).catch(() => null);\n      if (flush)")
s = s.replace("      await rename(tmpPath, path);", "      await rename(tmpPath, path).catch((e) => { if (!isNotFound(e)) throw e; });")
p.write_text(s)
'

# The control for the mutation above: keep the unique name, drop ONLY the
# exclusive-create flag. Green here means the uniqueness is what protects the
# key -- `wx` is a tripwire, not the guard.
run_control "drop only the exclusive-create flag, keep unique temp names" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("createWriteStream(tmpPath, { flags: \x27wx\x27 })", "createWriteStream(tmpPath, { flags: \x27w\x27 })")
p.write_text(s)
'

# ============================================================================
# phase-02 Risks: "Không verify content-length → file cụt tính là done"
# ============================================================================
run_mutation "stop comparing the byte count against Content-Length" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("if (expectedBytes !== undefined && bytes !== expectedBytes) {", "if (expectedBytes !== undefined && bytes < 0) {")
p.write_text(s)
'

# ============================================================================
# Keys are remote data: sec_user_id and aweme_id both come from Douyin.
# ============================================================================
run_mutation "stop checking that a key stays inside DATA_DIR" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
# Drops ONLY the traversal detection, leaving the empty-key and
# inside-the-root checks in place. A blunter edit (making `rel` a literal)
# either fails to typecheck or rejects every key, and a mutation that turns
# the whole suite red proves nothing about the specific guard.
s = s.replace(
  "if (rel.length === 0 || rel === \x27..\x27 || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {",
  "if (rel.length === 0) {")
p.write_text(s)
'

# ============================================================================
# phase-02 Risks: "Client tự retry → đốt ngân sách rate limit của enumerate"
# ============================================================================
# Douyin rate-limits by IP. A retry hidden here multiplies the request rate of
# an enumerate loop that believes it issues one call every 3-5 seconds (F2).
run_mutation "retry a non-200 inside the client" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace("      const res = await request(target, {\n        method: \x27GET\x27,", "      const res = await requestWithRetry(target, {\n        method: \x27GET\x27,")
s += """
async function requestWithRetry(
  target: URL,
  opts: Parameters<typeof request>[1],
): ReturnType<typeof request> {
  const first = await request(target, opts);
  if (first.statusCode !== 200) {
    await first.body.dump();
    return request(target, opts);
  }
  return first;
}
"""
p.write_text(s)
'

# ============================================================================
# F4: a 400 carries no cause. Claiming one sends an operator to rotate a
# perfectly good cookie because a profile happened to be private.
# ============================================================================
run_mutation "invent a cause for the fork catch-all 400 (F4)" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace(
  "`parser refused GET ${ROUTE_PREFIX}/${route} with HTTP 400. The fork answers 400 for every `",
  "`parser refused GET ${ROUTE_PREFIX}/${route} with HTTP 400: the cookie is banned or the profile is private. The fork answers 400 for every `")
p.write_text(s)
'

# ============================================================================
# A 5xx never came from the fork's catch-all, so it is not a Douyin verdict.
# Classifying it as a rejection sends phase 7 probing over a dead uvicorn.
# ============================================================================
run_mutation "classify a 5xx as a rejection instead of unavailable" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace("if (statusCode >= 500) {", "if (statusCode >= 900) {")
p.write_text(s)
'

# ============================================================================
# Douyin sends has_more as 1/0 (and sometimes "1"/"0"). Either a strict
# boolean read or a plain Boolean() coercion wedges the enumerate loop --
# the first stops every profile after page one, the second never stops at all.
# ============================================================================
run_mutation "read has_more as a strict boolean" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace("const hasMore = toBoolean(pick(data, \x27has_more\x27));", "const hasMore = pick(data, \x27has_more\x27) === true;")
p.write_text(s)
'

# ============================================================================
# freeBytes must count blocks available to an UNPRIVILEGED writer.
# ============================================================================
# `bfree` includes the root reserve. A phase-10 preflight built on it accepts
# a job that fills the disk for everyone else. On APFS the two numbers are
# identical, so the `df` comparison can never catch this -- the injected-statfs
# test is what makes the choice checkable on any machine.
run_mutation "count root-reserved blocks as free (bavail -> bfree)" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace("Number(stats.bavail) * Number(stats.bsize)", "Number(stats.bfree) * Number(stats.bsize)")
p.write_text(s)
'

# ============================================================================
# Symlink containment (the half `resolve()` cannot do).
# ============================================================================
run_mutation "skip the symlink containment check in put()" \
  "$STORAGE" "$STORAGE_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/storage/local-disk.storage.ts")
s = p.read_text()
s = s.replace(
  "    await mkdir(dirname(path), { recursive: true });\n    await this.assertParentInsideRoot(path, key);\n",
  "    await mkdir(dirname(path), { recursive: true });\n")
p.write_text(s)
'

# ============================================================================
# F4, the direction the 400 mutation does not cover: a Douyin-level refusal
# arrives as HTTP 200. Assuming success is as wrong as inventing a cause.
# ============================================================================
run_mutation "assume Douyin success in the profile probe (F4)" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace(
  "      nickname: typeof nickname === \x27string\x27 ? nickname : null,\n      statusCode: douyinStatusCode(data),",
  "      nickname: typeof nickname === \x27string\x27 ? nickname : null,\n      statusCode: 0,")
p.write_text(s)
'

# ============================================================================
# Enumerate-loop termination. Both of these wedge the loop silently while
# spending the F2 request budget.
# ============================================================================
run_mutation "coerce has_more with plain Boolean, so \"0\" reads as true" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace("const hasMore = toBoolean(pick(data, \x27has_more\x27));", "const hasMore = Boolean(pick(data, \x27has_more\x27));")
p.write_text(s)
'

run_mutation "fall back to cursor 0 on a page that claims more work" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace("if (hasMore && cursor === null) {", "if (!hasMore && cursor === null) {")
p.write_text(s)
'

# ============================================================================
# The no-retry rule against the REST OF THE PROCESS.
# ============================================================================
# Every other mutation here edits the file whose invariant it breaks. This one
# exists because the matching regression would NOT: any library calling
# setGlobalDispatcher(new RetryAgent(...)) turns one parser call into four
# without touching a line of parser.client.ts. Owning the dispatcher is what
# brings that invariant back inside a file a mutation can reach.
run_mutation "fall back to undici's global dispatcher" \
  "$CLIENT" "$CLIENT_TESTS" \
  py '
import pathlib
p = pathlib.Path("src/parser/parser.client.ts")
s = p.read_text()
s = s.replace("        dispatcher: this.dispatcher,\n", "")
p.write_text(s)
'

echo
if [ "$fail" -eq 0 ]; then
  echo "All phase-2 mutations applied; every mutation was caught and the control stayed green."
else
  echo "PHASE 2 MUTATION SUITE FAILED -- see above."
fi
exit "$fail"

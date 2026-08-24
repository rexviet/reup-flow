#!/usr/bin/env bash
# Plan phase 1 §1.5 + PATCH_NOTES item 5.
#
# Keeps the weekly `git pull upstream` cheap, and fails closed on the two ways
# the cookie patch can go silently half-applied.
#
# Three categories, two of them gated:
#
#   MODIFIED     lines changed in files upstream ALSO owns. The real
#                merge-conflict surface. Gated at PATCH_LINE_LIMIT (50).
#   OWNED CODE   new code files only we own (cookie_provider.py, etc.). Gated at
#                OWNED_CODE_LIMIT so that "move it into a file we own" cannot be
#                used to dodge the MODIFIED budget indefinitely.
#   TESTS + DOCS new files under tests/ and *.md. Reported, never gated --
#                upstream has no version of them so they cannot conflict, and
#                capping them would penalise exactly the behaviour we want.
#
# The phase DoD words this as `git diff upstream/main --stat <= 50`, i.e. the sum
# of all three. Taken literally that budget is spent before the first test is
# written, and it would push tests out of the fork where the phase spec §1.5 puts
# them. IDEA.md:158 words the same criterion as "~50 dòng thay đổi" (~50 lines
# *changed*), which is MODIFIED. So MODIFIED keeps the tight budget, new code we
# own gets its own ceiling, and tests/docs are free. Recorded in PATCH_NOTES.md
# and in the phase file -- not only here.

set -euo pipefail

FORK="${FORK:-src/Douyin_TikTok_Download_API}"
LIMIT="${PATCH_LINE_LIMIT:-50}"
OWNED_CODE_LIMIT="${OWNED_CODE_LIMIT:-250}"
UPSTREAM_URL="https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git"

cd "$(dirname "$0")/.."

# NOT `[ -d "$FORK/.git" ]`: in a real submodule checkout .git is a FILE
# containing `gitdir: ../../.git/modules/...`, so the -d test fails and this
# script would abort on every CI run while blaming a missing submodule.
git -C "$FORK" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "FAIL: $FORK is not a git repo. Run: git submodule update --init --recursive"
  exit 1
}

git -C "$FORK" remote get-url upstream >/dev/null 2>&1 \
  || git -C "$FORK" remote add upstream "$UPSTREAM_URL"
git -C "$FORK" fetch -q upstream

# A dirty fork makes both numbers lies: `git diff` cannot see untracked files, so
# new files we own count as 0 and TOTAL collapses onto MODIFIED. CI must never
# measure a dirty tree. Locally, ALLOW_DIRTY=1 to check work in progress.
if ! git -C "$FORK" diff --quiet || ! git -C "$FORK" diff --cached --quiet \
   || [ -n "$(git -C "$FORK" ls-files --others --exclude-standard)" ]; then
  if [ "${ALLOW_DIRTY:-0}" != "1" ]; then
    echo "FAIL: $FORK has uncommitted or untracked changes, so the line counts"
    echo "      below would be meaningless (git diff cannot see untracked files)."
    echo "      Commit the fork first, or re-run with ALLOW_DIRTY=1 to inspect WIP."
    exit 1
  fi
  echo "WARNING: fork tree is dirty (ALLOW_DIRTY=1). TOTAL undercounts untracked files."
  echo
fi

sum() { awk '{a+=$1; d+=$2} END {print a+d+0}'; }
added() { git -C "$FORK" diff upstream/main --numstat --diff-filter=A; }
# Tests and docs are free; everything else added is code we own.
is_free='$3 ~ /^tests\// || $3 ~ /\.md$/'

modified=$(git -C "$FORK" diff upstream/main --numstat --diff-filter=M | sum)
owned_code=$(added | awk "!($is_free)" | sum)
tests_docs=$(added | awk "$is_free" | sum)

echo "=== files upstream also owns (gated, limit $LIMIT) ==="
git -C "$FORK" diff upstream/main --stat --diff-filter=M
echo
echo "=== new code we own (gated, limit $OWNED_CODE_LIMIT) ==="
added | awk "!($is_free) {printf \"  %6d  %s\\n\", \$1+\$2, \$3}"
echo
echo "=== tests and docs (reported, not gated) ==="
added | awk "$is_free {printf \"  %6d  %s\\n\", \$1+\$2, \$3}"
echo
echo "modified-line surface : $modified  (limit $LIMIT)"
echo "new code we own       : $owned_code  (limit $OWNED_CODE_LIMIT)"
echo "tests + docs          : $tests_docs  (not gated)"
echo

fail=0

if [ "$modified" -gt "$LIMIT" ]; then
  echo "FAIL: patch touches $modified lines of upstream-owned code, limit $LIMIT."
  echo "      Shrink the patch or move logic into a file we own."
  fail=1
fi

if [ "$owned_code" -gt "$OWNED_CODE_LIMIT" ]; then
  echo "FAIL: new code we own is $owned_code lines, limit $OWNED_CODE_LIMIT."
  echo "      The cookie shim should stay a shim. If this is real growth, it"
  echo "      probably belongs in our own service, not inside the parser fork."
  fail=1
fi

# ---------------------------------------------------------------------------
# PATCH_NOTES item 5. The patch only holds while nothing outside
# cookie_provider.py reads the cookie out of the config dict.
#
# The narrow grep this replaced only matched headers["Cookie"] and
# headers.Cookie -- 2 of 9 plausible spellings. utils.py accesses config
# exclusively via .get() chaining, so the single most likely upstream refactor
# was invisible to it. This pattern covers bracket-indexing with either quote
# style, .get("Cookie"), and attribute access.
# ---------------------------------------------------------------------------
COOKIE_READ='\[[[:space:]]*["'"'"']Cookie["'"'"'][[:space:]]*\]|\.get\([[:space:]]*["'"'"']Cookie["'"'"']|headers\.Cookie'

# utils.py must never read the cookie -- it keeps its own yaml.safe_load copy of
# the config, so if it starts reading headers.Cookie the patch springs a silent
# leak: no error, just requests using the stale (empty) config value.
if grep -nE "$COOKIE_READ" "$FORK/crawlers/douyin/web/utils.py"; then
  echo "FAIL: utils.py now reads the cookie from config -- patch is leaking."
  echo "      See PATCH_NOTES.md item 5. utils.py needs cookie_provider too."
  fail=1
fi

# Any OTHER module in the package doing the same is the same bug in a new file.
for f in "$FORK"/crawlers/douyin/web/*.py; do
  case "$(basename "$f")" in
    cookie_provider.py|web_crawler.py|utils.py) continue ;;
  esac
  if grep -nE "$COOKIE_READ" "$f"; then
    echo "FAIL: $(basename "$f") reads the cookie from config."
    echo "      Route it through cookie_provider or add it to PATCH_NOTES."
    fail=1
  fi
done

# Positive assertion: web_crawler.py must still route through cookie_provider.
# Without this, reverting the patch is caught only by pytest -- and a revert that
# also drops the tests would otherwise sail through.
if ! grep -qE 'cookie_provider|resolve_cookie|get_cookie\(' "$FORK/crawlers/douyin/web/web_crawler.py"; then
  echo "FAIL: web_crawler.py no longer references cookie_provider."
  echo "      The Cookie header has reverted to the config value."
  fail=1
fi

# ---------------------------------------------------------------------------
# The scrub must hold. Checking for the PRESENCE of an empty Cookie is not
# enough -- a second, non-empty Cookie key elsewhere in the file would pass.
# So: fail on ANY Cookie value that is not the empty string.
# ---------------------------------------------------------------------------
CFG="$FORK/crawlers/douyin/web/config.yaml"
if bad=$(grep -nE '^[[:space:]]*Cookie:' "$CFG" \
         | grep -vE ':[[:space:]]*Cookie:[[:space:]]*(""|'"''"')[[:space:]]*$'); then
  echo "FAIL: non-empty Cookie in $CFG:"
  echo "$bad" | sed 's/^/      /' | cut -c1-120
  echo "      Cookies belong in Redis or DOUYIN_COOKIE_FALLBACK, never in a tracked file."
  fail=1
fi
if ! grep -qE '^[[:space:]]*Cookie:' "$CFG"; then
  echo "FAIL: no Cookie key at all in $CFG -- upstream layout changed, re-audit."
  fail=1
fi

[ "$fail" -eq 0 ] || exit 1
echo "OK: patch within budget; cookie confined to cookie_provider; config scrubbed."

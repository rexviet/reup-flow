#!/usr/bin/env bash
#
# EVERY mutation harness in the repo, in one command, with the counts declared.
#
# plan.md §5 has nine enforcement rules, each written down because a mutation
# harness on this repo has already lied in that specific way. Each per-phase
# harness enforces those rules WITHIN itself. This script exists for the failure
# mode none of them can see, because it is a property of the SET rather than of
# any member:
#
#   A HARNESS CAN GO HOLLOW AND NOTHING WILL SAY SO.
#
# That is not hypothetical. Phase 5 changed the `LANES` factory and the WHERE
# clause of `finalizeSettled` without updating `phase-04.sh`, so two of phase 4's
# mutations stopped applying. `phase-04.sh` exited 1 with "matched nothing" --
# correctly, loudly, and TO NOBODY, because CI only ever ran `phase-01.sh`. Five
# subsequent phases each reported a clean run of their own suite while two of
# phase 4's guards were unwatched. Phase 6 found it by hand.
#
# So this script does four things no individual harness can:
#
#   A. RUNS THEM ALL. A guard that is not executed in CI is a guard nobody is
#      watching, whatever its script says.
#   B. CHECKS THE COUNTS. Every harness has a DECLARED number of must-fail
#      mutations and must-stay-green controls below, and the outcome lines are
#      counted against it. A mutation that is deleted, commented out, or that
#      silently stops applying makes the arithmetic fail -- so "hollow" is a
#      build failure rather than a smaller number nobody compares.
#   C. TREATS AN EMPTY APPLICATION AS A HARD FAILURE, at this level too. A
#      harness reporting "matched nothing" fails the build even if it somehow
#      exits 0, and it is reported as its own category rather than folded into
#      "some mutation failed" -- the two need different fixes.
#   D. VERIFIES THE TREE IS UNCHANGED afterwards. Rule 5 asks each harness to
#      restore itself from a pristine snapshot on any exit; this is the
#      independent check on that promise. A harness whose own restore is broken
#      leaves a mutation in place, and every measurement after it -- including
#      every OTHER harness in this run -- is taken against a poisoned build.
#
# Usage, from the repo root:
#
#   scripts/run-mutations.sh                   # all harnesses
#   scripts/run-mutations.sh 04 07             # downloader phases 04 and 07
#   scripts/run-mutations.sh localization/03   # one harness of another service
#   scripts/run-mutations.sh 'localization/*'  # every harness of one service
#
# A bare phase ("04", "infra") means the downloader's, which is what it meant
# before the manifest named a service. Any other service needs `dir/phase`.
#
# The downloader harnesses need the compose stack (Postgres + Redis) up:
#   docker compose up -d
# The localization harnesses need `uv sync` in services/localization.
#
# WHEN YOU ADD OR REMOVE A MUTATION, update EXPECT below in the same commit.
# The number failing is the point: it is what turns "somebody quietly removed a
# guard" from a thing nobody notices into a red build.

set -u

cd "$(dirname "$0")/.." || exit 1

# Where each service keeps its harnesses. A row naming a service that is not
# listed here is a hard failure, not a skipped row.
harness_dir() {
  case "$1" in
    downloader) echo services/downloader/test/mutations ;;
    localization) echo services/localization/tests/mutations ;;
    renderer) echo services/renderer/tests/mutations ;;
    *) return 1 ;;
  esac
}

# dir:phase:must-fail-mutations:must-stay-green-controls
#
# Counted from the harnesses as of phase 7, plus phase-05.sh's BLOCK 12 (the
# 图文 image-post guard, added 2026-08-26: +4 mutations, +2 controls).
# Totals: 139 mutations, 17 controls, plus `infra` (the localization compose
# guards, added with the video-localization plan's phase 00): +11, +2 => 150, 19.
# `12` (localization_outbox atomicity, same plan): +1, +0 => 151, 19.
# `11` (S3Storage, needs S3_ENDPOINT + the downloader MinIO key in the
# environment): +5, +0 => 156, 19.
# `localization:03` (foundation: workflow import allowlist, codec guard, secret
# canary, contract schema drift, scanner bypasses, payload pre-check, 2xx WAV, source/
# key prefix): +11, +2 => 167, 21.
# `downloader:infra` gains the localization processes' compose guards (the media
# worker holds no provider key, the starter holds no object-store key, no worker
# publishes a port): +3, +0 => 14, 2; the workers' sized /tmp and stop grace period:
# +2 => 16, 2.
# `localization:04` (starter: deterministic id, generation, throttle, eligibility,
# backoff, dead, no lock across the network, settle by generation, voice must
# exist; workers: concurrency per queue; workflow: replay, once-per-stage rerun,
# 2a || 2b; preparation activities; the image; tracing; grants), then the review fixes (bounded frame extraction, voice wiring, worker
# supervision and shutdown, heartbeats, Demucs timeout and cancel, STT downmix,
# SKIP LOCKED, the throttle under lagging visibility, outage handling), then parallel shutdown, sibling
# cancel, end-of-video seek, process-group kill and the release statement's
# grants: +47, +2. Then four for the subtitle region padding and time-based gap
# bridging (registered, not yet run): +51, +2. Then three for the vision request's
# answer-only mode and token cap (registered, not yet run): +54, +2. Then four for tolerating one malformed OCR
# answer per frame while failing when most are (registered, not yet run): +58, +2. Then, for the cover fitted to the text's
# pixels (registered, not yet run): the two padding mutations are replaced and
# seven added, +63, +2; renderer:07 gains one for the minimum cover width: 66.
# The pixel fitting was then removed (measured worse on real frames) and replaced by
# a consensus over the model's boxes: 7 mutations out, 6 in (registered, not yet run):
# localization:04 is 62; renderer:07 gains one for the drawn cover: 67.
# `localization:09` (the cost OpenRouter reports, on the span and in llm_calls; registered,
# not yet run): 10 mutations, 0 controls.
# `localization:05` (Localizer: output covers every segment, video text is data,
# glossary first-writer-wins / cap / contact and digit guards, contacts needing a
# person, syllable counting, voice rate key and cache, queue pinning, replay marker,
# usage limit, lost-input rerun once, worker wiring; then the conversation size
# guard and segment cap, glossary provenance, wrapped stored term, voice rate bounds,
# number edge cases, voice-rate scaling by speed and bound, retryable bad reading,
# conversation margin, length-check cap, answer-only budget, slot extension): +64, +2.
# `localization:06` (dubbing: a TTS hash is sent at most once (the cache, the plan's
# de-duplication, each part of the hash, the write after the answer), the Dubber's tts
# tool pinned to the tts queue, its clamp / rewrite limit / output check / untrusted
# block, synthesis and the track never reach an agent, the stretch table and overflow
# flag, a rewrite cannot add a contact, the track's placement / stretch / ducking /
# loudness / length, the stage's replay marker and one regeneration per lost stage,
# worker wiring, a failed batch is acted on at once and cancels the rest, single-flight
# per clip, the regenerated script in the result, the mix cut at the video end, true
# peak, usage limits scaled, Dubber model fallback): +61, +3.
# `downloader:infra` gains the render worker's compose guards (shared memory, no
# localization key, RENDER_CONCURRENCY, Chrome checksum in the image, one font setting
# for both sides): +5 => 21, 2.
# `renderer:07` (captions and the renderer: a spec is rendered once and keyed by its
# hash, audio and font are the ones the spec names, nothing outlives a render, the asset
# server's token / loopback / path guard, the schema and the hash agree with Python, one
# render at a time, the trace interceptor, the layout's cover / line breaking / cue timing /
# one translation / what the spec carries, the font measured is the font drawn): +37, +4.
# A shared render belongs to all its callers (the last one to leave stops it), the deadline
# grows with the video, the frame is bounded by area: +10, +0 (registered, not yet run).
# `localization:08` (review, rerun routing, publish, the characters a caption cannot show,
# the Reviewer): 55 mutations, 2 controls (registered, not yet run). Then nine for the rerun
# that cannot fix what the review found (time to the end of the video, the slot, a problem
# that came back, the rerun question's replay marker, the activity and its wiring;
# registered, not yet run): 64. Then nine for the voice's rate measured beside the
# ingest (overlap, its marker, a failure of either stopping the other, 2a and 2b waiting
# for it, measured once; registered, not yet run): 73.
# The phase 0 spike mutations (test/spike-cookie-swap/mutations, and the SKIP
# LOCKED / compose pair inlined in ci.yml) are deliberately NOT here: they need
# a built parser image or a compose file edit, and are gated separately in CI.
EXPECT="
downloader:01:8:0
downloader:02:15:1
downloader:03:18:2
downloader:04:20:2
downloader:05:36:4
downloader:06:14:3
downloader:07:28:5
downloader:infra:22:2
downloader:11:5:0
downloader:12:1:0
localization:03:11:2
localization:04:62:2
localization:05:64:2
localization:06:61:3
localization:08:73:2
localization:09:10:0
renderer:07:67:5
"

# Files any harness in the set is allowed to touch. Checksummed before and after
# each run (guard D). Kept as a list rather than "the whole repo" so an unrelated
# edit in another service cannot make this report a poisoned tree.
#
# Python, TSX, JSON, fonts and shell are fingerprinted too, because the
# localization and renderer harnesses mutate those. Virtualenvs, dependency
# trees and tool caches are pruned: they change on their own and are not source.
TRACKED_PATHS="services/downloader/src services/downloader/migrations docker-compose.yml infra services/localization services/renderer/src contracts assets/fonts"

selected=("$@")

tree_fingerprint() {
  # `find | sort` so the order is stable; content hashed, not mtime, because a
  # restore that rewrites identical bytes is a correct restore.
  # shellcheck disable=SC2086
  find $TRACKED_PATHS \
    \( -name .venv -o -name node_modules -o -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -prune -o \
    -type f \( -name '*.ts' -o -name '*.tsx' -o -name '*.py' -o -name '*.json' -o -name '*.ttf' -o -name '*.sh' \
      -o -name '*.sql' -o -name '*.yml' -o -name Dockerfile \) -print 2>/dev/null \
    | sort \
    | xargs shasum \
    | shasum \
    | cut -d' ' -f1
}

wanted() {
  local dir=$1 phase=$2
  [ "${#selected[@]}" -eq 0 ] && return 0
  local want
  for want in "${selected[@]}"; do
    case "$want" in
      */*) [ "$want" = "$dir/$phase" ] && return 0
           [ "$want" = "$dir/*" ] && return 0 ;;
      *)   [ "$dir" = downloader ] && [ "$want" = "$phase" ] && return 0 ;;
    esac
  done
  return 1
}

overall=0
summary=""
ran=0

for row in $EXPECT; do
  dir=${row%%:*}
  rest=${row#*:}
  phase=${rest%%:*}
  rest=${rest#*:}
  expect_mutations=${rest%%:*}
  expect_controls=${rest##*:}

  wanted "$dir" "$phase" || continue

  # What a harness is called in the output. The downloader's keep the name they
  # had before the manifest named a service.
  if [ "$dir" = downloader ]; then label="phase-$phase"; else label="$dir/phase-$phase"; fi

  if ! hdir=$(harness_dir "$dir"); then
    echo "::error::EXPECT row '$row' names the unknown service '$dir'."
    echo "Add it to harness_dir() or fix the row; an unlocatable harness is a"
    echo "harness nobody is running."
    overall=1
    summary="$summary\n$label  UNKNOWN-SERVICE"
    continue
  fi
  script="$hdir/phase-$phase.sh"

  if [ ! -f "$script" ]; then
    echo "::error::$script is declared in EXPECT but does not exist."
    echo "A harness named in the manifest and missing from the tree is the exact"
    echo "shape this script exists to catch. Restore it or remove the row."
    overall=1
    summary="$summary\n$label  MISSING"
    continue
  fi

  before=$(tree_fingerprint)
  log=$(mktemp)
  echo
  echo "################################################################"
  echo "# ${label}.sh  (expecting $expect_mutations mutations, $expect_controls controls)"
  echo "################################################################"
  bash "$script" 2>&1 | tee "$log"
  rc=${PIPESTATUS[0]}
  after=$(tree_fingerprint)

  caught=$(grep -cE '^OK: caught' "$log" || true)
  green=$(grep -cE '^OK: still green, as required\.' "$log" || true)
  stayed_green=$(grep -cE '^FAIL: (suite|lint) stayed GREEN' "$log" || true)
  control_red=$(grep -cE '^FAIL: control ' "$log" || true)
  empty=$(grep -cE '^ERROR: mutation matched nothing' "$log" || true)
  no_tests=$(grep -cE '^ERROR: the test pattern ' "$log" || true)
  hung=$(grep -cE '^OK: caught, but by HANGING' "$log" || true)
  restored=$(grep -cE '^NOTE: restored from the pristine snapshot' "$log" || true)
  rm -f "$log"

  status="OK"

  # (C) An empty application is its own category and its own hard failure.
  if [ "$empty" -gt 0 ]; then
    echo "::error::$label: $empty mutation(s) MATCHED NOTHING."
    echo "The code shape moved and those mutations no longer apply. They are not"
    echo "passes -- they are guards that were never tested. This is phase 4's"
    echo "harness going stale under phase 5's refactor, happening again."
    status="STALE"
    overall=1
  fi

  if [ "$no_tests" -gt 0 ]; then
    echo "::error::$label: $no_tests mutation(s) selected NO TESTS (rule 2/8)."
    echo "Exit code alone cannot distinguish that from 'caught'. Fix the pattern."
    status="NO-TESTS"
    overall=1
  fi

  if [ "$stayed_green" -gt 0 ] || [ "$control_red" -gt 0 ]; then
    echo "::error::$label: $stayed_green mutation(s) stayed green, $control_red control(s) went red."
    status="RED"
    overall=1
  fi

  # (B) THE COUNT CHECK. Every declared mutation must have produced exactly one
  # outcome line, and so must every declared control. A deleted mutation shows up
  # here and nowhere else: the harness would exit 0, report fewer catches, and
  # look like a good run.
  outcomes=$((caught + stayed_green + empty + no_tests))
  if [ "$outcomes" -ne "$expect_mutations" ]; then
    echo "::error::$label: expected $expect_mutations must-fail mutations, saw $outcomes outcome(s)."
    echo "A harness that reports FEWER results than it declares is a harness that"
    echo "quietly lost a guard. If the change was intentional, update EXPECT in"
    echo "scripts/run-mutations.sh in the same commit."
    status="COUNT"
    overall=1
  fi
  control_outcomes=$((green + control_red))
  if [ "$control_outcomes" -ne "$expect_controls" ]; then
    echo "::error::$label: expected $expect_controls must-stay-green controls, saw $control_outcomes."
    echo "Controls are what say WHICH mechanism carries an invariant (rule 4)."
    echo "Losing one silently turns a proven guard back into an assumed one."
    status="COUNT"
    overall=1
  fi

  # (D) The independent check on rule 5's promise.
  if [ "$before" != "$after" ]; then
    echo "::error::$label: the source tree CHANGED across this harness run."
    echo "A mutation is still in place. Every measurement after this point --"
    echo "including the other harnesses in this run -- was taken against a"
    echo "poisoned build and means nothing. Fix the harness's restore path."
    status="POISONED"
    overall=1
  fi
  if [ "$restored" -gt 0 ]; then
    # Not fatal: the pristine trap is SUPPOSED to fire when a harness is killed.
    # On a normal run it means a mutation's own restore did not work, and rule 5
    # says a silent restore is indistinguishable from no restore -- so it is said
    # out loud here too rather than only inside the harness's own output.
    echo "::warning::$label: the pristine snapshot had to restore something."
    echo "On an uninterrupted run that means a mutation's own restore is broken."
  fi

  if [ "$rc" -ne 0 ] && [ "$status" = "OK" ]; then
    echo "::error::$label: exited $rc with no recognised failure line."
    echo "Either the harness failed before its first mutation (a broken build, a"
    echo "Postgres that is down) or it has grown a failure mode this script does"
    echo "not know how to read. Both need a human."
    status="EXIT-$rc"
    overall=1
  fi

  summary="$summary\n$label  $status  caught=$caught/$expect_mutations controls-green=$green/$expect_controls empty=$empty no-tests=$no_tests hung=$hung"
  ran=$((ran + 1))
done

if [ "$ran" -eq 0 ]; then
  echo "::error::no harness ran. A run that measures nothing must never exit 0."
  exit 1
fi

echo
echo "================================================================"
echo "MUTATION SUITE SUMMARY"
# shellcheck disable=SC2059
printf "$summary\n"
echo "================================================================"
if [ "$overall" -eq 0 ]; then
  echo "ALL HARNESSES: every declared mutation applied and was caught, every"
  echo "declared control stayed green, and no harness left the tree modified."
else
  echo "FAILURES ABOVE. Read the category, because they need different fixes:"
  echo "  STALE     -- a mutation no longer applies. The guard is UNTESTED."
  echo "  COUNT     -- fewer outcomes than declared. A guard was lost."
  echo "  NO-TESTS  -- the pattern selected nothing. Neither pass nor fail."
  echo "  RED       -- a mutation stayed green, or a control went red."
  echo "  POISONED  -- a mutation survived the run. Nothing after it is valid."
  echo "See plan.md §5 for why each of these is a hard failure and not a warning."
fi
exit "$overall"

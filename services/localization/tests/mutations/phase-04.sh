#!/usr/bin/env bash
#
# Mutation suite for the starter, worker and preparation-stage guards:
#
#   - one workflow per video and generation          (tests/starter)
#   - a re-run needs a new generation                (tests/starter)
#   - the throttle, eligibility, backoff and dead    (tests/starter)
#     rules of the outbox starter
#   - no row lock across the network, and a settle   (tests/starter)
#     never overwrites a re-run
#   - the voice must exist on the provider           (tests/starter, tests/providers)
#   - every task queue honours its concurrency limit (tests/workers)
#   - replay of recorded histories                   (tests/replay)
#   - a lost artifact re-runs its stage exactly once (tests/workflows)
#   - the preparation activities' own rules          (tests/workflows)
#   - the image: pinned base, verified weights,      (tests/test_dockerfile.py,
#     weights-only loader                             tests/test_demucs_safe.py)
#   - the starter's database grants                  (tests/starter/test_grants.py)
#
# A guard is evidence only if breaking its invariant turns it red, and the
# controls say WHICH mechanism carries it. Protocol and rules: see
# scripts/lib/mutation-protocol.sh.
#
# Run from anywhere:  bash services/localization/tests/mutations/phase-04.sh
# Needs `uv sync` in services/localization, the compose Postgres on localhost:5432
# (the starter tests apply the real migrations to a throwaway database), and
# ffmpeg. Temporal runs in-process.

set -u

cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=../../../../scripts/lib/mutation-protocol.sh
source ../../scripts/lib/mutation-protocol.sh

STARTER=src/localization/starter/outbox_starter.py
WORKFLOW=src/localization/workflows/video_localization.py
RUN=src/localization/workers/run.py
PERSIST=src/localization/activities/persist.py
TRANSCRIBE=src/localization/activities/transcribe.py
DETECT=src/localization/activities/detect_subtitles.py
FFMPEG=src/localization/activities/ffmpeg.py
TRACING=src/localization/observability/tracing.py
VOICES=src/localization/providers/voices.py
DEMUCS_SAFE=src/localization/providers/demucs_safe.py
DOCKERFILE=Dockerfile
GRANTS=migrations/261026-outbox-starter-grants.sql

T_START=tests/starter/test_outbox_starter.py
T_GRANTS=tests/starter/test_grants.py
T_PREP=tests/workflows/test_prep_stage.py
T_CONC=tests/workers/test_queue_concurrency.py
T_REPLAY=tests/replay/test_replay.py
T_VOICES=tests/providers/test_voices.py
T_TRACING=tests/test_tracing.py
T_DOCKER=tests/test_dockerfile.py
T_SAFE=tests/test_demucs_safe.py

mp_init "$STARTER" "$WORKFLOW" "$RUN" "$PERSIST" "$TRANSCRIBE" "$DETECT" "$FFMPEG" "$TRACING" \
  "$VOICES" "$DEMUCS_SAFE" "$DOCKERFILE" "$GRANTS"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)

mp_baseline "$T_START $T_GRANTS $T_PREP $T_CONC $T_REPLAY $T_VOICES $T_TRACING $T_DOCKER $T_SAFE"

# --- 1. the workflow id is no longer the video and the generation ---------------
mp_mutation "the workflow id carries a random suffix instead of being deterministic"
apply_mutation "$STARTER" \
  '    return f"video-{aweme_id}-g{generation}"' \
  '    return f"video-{aweme_id}-g{generation}-{__import__("uuid").uuid4().hex}"' \
  && expect_red "$T_START" \
    "test_two_starters_start_each_generation_exactly_once" \
    "test_a_row_started_twice_is_one_workflow" \
    "test_workflow_id_is_the_video_and_the_generation"
restore

# --- 2. the generation is not part of the id: a re-run cannot start --------------
mp_mutation "the workflow id ignores the generation, so a bumped generation starts nothing"
apply_mutation "$STARTER" \
  '    return f"video-{aweme_id}-g{generation}"' \
  '    return f"video-{aweme_id}-g1"' \
  && expect_red "$T_START" "test_bumping_the_generation_runs_a_finished_video_again"
restore

# --- 3. the throttle ------------------------------------------------------------
mp_mutation "the starter ignores MAX_OPEN_WORKFLOWS"
apply_mutation "$STARTER" \
  '        if await self._open_workflows() >= self._load().max_open_workflows:' \
  '        if False:' \
  && expect_red "$T_START" "test_nothing_is_started_while_max_open_workflows_are_running"
restore

# --- 4. a path that is not s3:// is accepted -------------------------------------
mp_mutation "the starter no longer rejects a storage_path that is not an s3:// URL"
apply_mutation "$STARTER" '    if match is None:' '    if False:' \
  && expect_red "$T_START" "test_a_row_that_can_never_be_valid_is_rejected_and_the_next_row_still_runs"
restore

# --- 5. rows from another bucket are accepted ------------------------------------
mp_mutation "the starter no longer checks the bucket of the storage_path"
apply_mutation "$STARTER" '    if match["bucket"] != bucket:' '    if False:' \
  && expect_red "$T_START" "test_a_row_that_can_never_be_valid_is_rejected_and_the_next_row_still_runs"
restore

# --- 6. a key outside source/ is accepted ----------------------------------------
mp_mutation "the starter no longer requires the key to live under source/"
apply_mutation "$STARTER" \
  '        return validate_source_key(match["key"])' \
  '        return match["key"]' \
  && expect_red "$T_START" "test_a_row_that_can_never_be_valid_is_rejected_and_the_next_row_still_runs"
restore

# --- 7. a failing row is retried forever ------------------------------------------
mp_mutation "a row that keeps failing never becomes dead"
apply_mutation "$STARTER" "THEN 'dead' ELSE state END" "THEN state ELSE state END" \
  && expect_red "$T_START" "test_a_row_that_fails_to_dispatch_five_times_is_dead"
restore

# --- 8. the backoff is not exponential ---------------------------------------------
mp_mutation "the dispatch backoff is a constant"
apply_mutation "$STARTER" \
  '    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempts_so_far)' \
  '    return BACKOFF_BASE_SECONDS' \
  && expect_red "$T_START" "test_a_row_that_fails_to_dispatch_five_times_is_dead"
restore

# --- 9. the claim lock is held while the workflow is started -------------------------
mp_mutation "the claim transaction stays open across start_workflow"
apply_mutation "$STARTER" \
  $'        async with conn.transaction():\n            cursor = await conn.execute(CLAIM_SQL, {"minutes": CLAIM_MINUTES})\n            return await cursor.fetchone()' \
  $'        await conn.execute("BEGIN")\n        cursor = await conn.execute(CLAIM_SQL, {"minutes": CLAIM_MINUTES})\n        return await cursor.fetchone()' \
  && expect_red "$T_START" "test_no_row_lock_is_held_while_the_workflow_is_started"
restore

# --- 10. a settle overwrites a row that was re-run meanwhile -----------------------
mp_mutation "the dispatched update no longer checks the generation it claimed"
apply_mutation "$STARTER" \
  $'last_error = NULL\n WHERE aweme_id = %s AND generation = %s AND state = \'pending\'' \
  $'last_error = NULL\n WHERE aweme_id = %s AND %s::int IS NOT NULL AND state = \'pending\'' \
  && expect_red "$T_START" "test_a_generation_bumped_during_the_start_is_not_overwritten"
restore

# --- 11. the voice is not checked against the provider ------------------------------
mp_mutation "the starter dispatches without asking the provider whether the voice exists"
apply_mutation "$STARTER" \
  '                await catalog.ensure_exists(voice.voice_id)' \
  '                pass' \
  && expect_red "$T_START" "test_a_voice_the_provider_does_not_list_is_never_dispatched"
restore

mp_mutation "the voice catalog accepts a voice that is not in the list"
apply_mutation "$VOICES" '        if voice_id not in ids:' '        if False:' \
  && expect_red "$T_VOICES" "test_a_listed_voice_passes_and_an_unlisted_one_is_refused"
restore

# --- 13. the concurrency limit is not applied ------------------------------------------
mp_mutation "build_worker no longer applies the queue's concurrency limit"
apply_mutation "$RUN" \
  '        max_concurrent_activities=spec.limit,' \
  '        max_concurrent_activities=None,' \
  && expect_red "$T_CONC" "test_a_queue_never_runs_more_activities_than_its_limit"
restore

# --- 14. replay: the order of commands changes without patched() ----------------------
mp_mutation "2b is started before 2a with no workflow.patched marker"
apply_mutation "$WORKFLOW" \
  $'        transcribing = workflow.start_activity(\n            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n        )\n        detecting = workflow.start_activity(\n            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n        )\n        transcript = await self._settle(' \
  $'        detecting = workflow.start_activity(\n            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n        )\n        transcribing = workflow.start_activity(\n            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n        )\n        transcript = await self._settle(' \
  && expect_red "$T_REPLAY" "test_every_recorded_history_still_replays"
restore

# --- 15. a lost artifact re-runs its stage more than once -------------------------------
mp_mutation "the once-per-stage budget for re-running a producing stage is gone"
apply_mutation "$WORKFLOW" \
  '                if stage not in REGENERABLE_STAGES or stage in self._regenerated:' \
  '                if stage not in REGENERABLE_STAGES:' \
  && expect_red "$T_PREP" "test_a_second_loss_fails_the_workflow_instead_of_looping"
restore

# --- 16. 2a and 2b stop running at the same time -----------------------------------------
mp_mutation "transcribe is awaited before detect_subtitles is started"
apply_mutation "$WORKFLOW" \
  $'        detecting = workflow.start_activity(\n' \
  $'        await transcribing\n        detecting = workflow.start_activity(\n' \
  && expect_red "$T_PREP" "test_transcribe_and_detect_run_at_the_same_time"
restore

# --- 17. preparation activities ------------------------------------------------------------
mp_mutation "persist_json writes under any prefix"
apply_mutation "$PERSIST" '        if not key.startswith(PERSIST_PREFIX):' '        if False:' \
  && expect_red "$T_PREP" "test_persist_json_writes_under_work_and_refuses_anywhere_else"
restore

mp_mutation "a pause of 4 seconds, not 400 ms, ends a transcript segment"
apply_mutation "$TRANSCRIBE" 'SILENCE_GAP_MS = 400' 'SILENCE_GAP_MS = 4000' \
  && expect_red "$T_PREP" "test_a_pause_over_400ms_ends_a_segment_and_exactly_400ms_does_not"
restore

mp_mutation "a missing video stream is a retryable failure"
apply_mutation "$FFMPEG" \
  '        raise PermanentProviderError("no_video_stream")' \
  '        raise TransientProviderError("no_video_stream")' \
  && expect_red "$T_PREP" \
    "test_a_file_with_only_cover_art_has_no_video_stream" \
    "test_ingest_without_a_video_stream_is_a_permanent_failure"
restore

mp_mutation "cover art counts as a video stream"
apply_mutation "$FFMPEG" \
  ' and not (s.get("disposition") or {}).get("attached_pic")' '' \
  && expect_red "$T_PREP" "test_a_file_with_only_cover_art_has_no_video_stream"
restore

mp_mutation "a single stray detection counts as a subtitle band"
apply_mutation "$DETECT" 'MIN_BAND_FRAMES = 2' 'MIN_BAND_FRAMES = 1' \
  && expect_red "$T_PREP" "test_a_single_detection_is_not_a_subtitle_and_none_means_no_region"
restore

# --- 18. tracing ---------------------------------------------------------------------------------
mp_mutation "the session tag is written under another attribute name"
apply_mutation "$TRACING" 'SESSION_ATTRIBUTE = "session.id"' 'SESSION_ATTRIBUTE = "session"' \
  && expect_red "$T_TRACING" "test_the_session_tag_lands_on_the_current_span_as_the_video_id"
restore

# --- 19. the image ----------------------------------------------------------------------------------
mp_mutation "the Demucs weights are no longer checked against their sha256"
apply_mutation "$DOCKERFILE" \
  $'\n && echo "$DEMUCS_WEIGHTS_SHA256  /opt/torch/hub/checkpoints/955717e8-8726e21a.th" | sha256sum -c -' \
  '' \
  && expect_red "$T_DOCKER" "test_the_demucs_weights_are_verified_against_a_full_sha256_or_the_build_fails"
restore

mp_mutation "the base image loses its digest"
apply_mutation "$DOCKERFILE" \
  'python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58' \
  'python3.12-bookworm-slim' \
  && expect_red "$T_DOCKER" "test_every_base_image_is_pinned_by_digest"
restore

mp_mutation "the launcher lets a caller turn weights_only off"
apply_mutation "$DEMUCS_SAFE" \
  '        kwargs["weights_only"] = True' \
  '        kwargs.setdefault("weights_only", True)' \
  && expect_red "$T_SAFE" "test_every_load_is_weights_only_whatever_the_caller_asked"
restore

# --- 20. the starter's grants ----------------------------------------------------------------------------
mp_mutation "the starter role is granted INSERT and DELETE on the outbox"
apply_mutation "$GRANTS" \
  'GRANT SELECT, UPDATE ON localization_outbox TO localization;' \
  'GRANT SELECT, INSERT, UPDATE, DELETE ON localization_outbox TO localization;' \
  && expect_red "$T_GRANTS" "test_the_starter_can_do_nothing_else"
restore

# --- control A: a constant that is not an invariant ------------------------------------------------------
mp_mutation "CONTROL: the starter polls an idle outbox four times as often"
apply_mutation "$STARTER" 'IDLE_SLEEP_SECONDS = 2.0' 'IDLE_SLEEP_SECONDS = 0.5' \
  && expect_green "$T_START" "idle-sleep"
restore

# --- control B: the SAME reorder as mutation 14, guarded by patched() -------------------------------------
# Shows the replay guard rejects an UNGUARDED reorder, not any edit of the
# workflow: with the marker, histories recorded before it still take the old path.
mp_mutation "CONTROL: 2b is started before 2a, guarded by workflow.patched"
apply_mutation "$WORKFLOW" \
  $'        transcribing = workflow.start_activity(\n            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n        )\n        detecting = workflow.start_activity(\n            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n        )\n        transcript = await self._settle(' \
  $'        if workflow.patched("detect-before-transcribe"):\n            detecting = workflow.start_activity(\n                "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n            )\n            transcribing = workflow.start_activity(\n                "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n            )\n        else:\n            transcribing = workflow.start_activity(\n                "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n            )\n            detecting = workflow.start_activity(\n                "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n            )\n        transcript = await self._settle(' \
  && expect_green "$T_REPLAY" "patched-reorder"
restore

mp_finish

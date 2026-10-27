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
INGEST=src/localization/activities/ingest.py
DEMUCS=src/localization/providers/demucs.py
VISION_OCR=src/localization/providers/vision_ocr.py
REVIEW_RENDER=src/localization/activities/review_render.py
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
T_SUPERVISE=tests/workers/test_supervise.py
T_OCR=tests/providers/test_ocr_demucs_llm.py
T_REVIEW=tests/activities/test_review_render.py
T_DEMUCS=tests/providers/test_demucs_runtime.py

mp_init "$STARTER" "$WORKFLOW" "$RUN" "$PERSIST" "$INGEST" "$DEMUCS" "$VISION_OCR" "$REVIEW_RENDER" "$TRANSCRIBE" "$DETECT" "$FFMPEG" "$TRACING" \
  "$VOICES" "$DEMUCS_SAFE" "$DOCKERFILE" "$GRANTS"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)

mp_baseline "$T_START $T_GRANTS $T_PREP $T_CONC $T_REPLAY $T_VOICES $T_TRACING $T_DOCKER $T_SAFE $T_SUPERVISE $T_DEMUCS $T_OCR $T_REVIEW"

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
  $'        transcribing = workflow.start_activity(\n            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n        )\n        detecting = workflow.start_activity(\n            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n        )\n        await workflow.wait(' \
  $'        detecting = workflow.start_activity(\n            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n        )\n        transcribing = workflow.start_activity(\n            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n        )\n        await workflow.wait(' \
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
apply_mutation "$PERSIST" '        if not PERSIST_KEY_RE.fullmatch(key):' '        if False:' \
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
mp_mutation "the starter role may read every column of videos"
apply_mutation "$GRANTS" \
  'GRANT SELECT (aweme_id, sec_user_id, storage_path) ON videos TO localization;' \
  'GRANT SELECT ON videos TO localization;' \
  && expect_red "$T_GRANTS" "test_the_starter_can_do_nothing_else"
restore

mp_mutation "the starter role may update the generation of an outbox row"
apply_mutation "$GRANTS" \
  'GRANT UPDATE (state, next_attempt_at,' \
  'GRANT UPDATE (generation, state, next_attempt_at,' \
  && expect_red "$T_GRANTS" "test_the_starter_can_do_nothing_else"
restore

# --- review fixes: disk, voice wiring, supervision, shutdown, throttle, outages ----------------------
mp_mutation "frames are extracted for the whole video at once instead of a chunk at a time"
apply_mutation "$DETECT" '                        count=self._chunk_frames,' '                        count=10**9,' \
  && expect_red "$T_PREP" "test_frames_on_disk_never_exceed_one_chunk_however_long_the_video"
restore

mp_mutation "frames are sent to the vision model at source size"
apply_mutation "$DETECT" \
  '    factor = min(1.0, OCR_MAX_SIDE / max(width, height))' '    factor = 1.0' \
  && expect_red "$T_PREP" "test_boxes_are_returned_in_source_pixels_when_frames_were_scaled"
restore

mp_mutation "the io queue has no concurrency limit"
apply_mutation "$RUN" $'                settings.io_concurrency,\n' '' \
  && expect_red "$T_CONC" "test_a_queue_never_runs_more_activities_than_its_limit"
restore

mp_mutation "a VoiceStudio voice is dispatched unchecked when VOICESTUDIO_URL is unset"
apply_mutation "$STARTER" \
  '        raise ConfigError("VOICESTUDIO_URL must be set to validate VOICE_ID")' \
  '        return None' \
  && expect_red "$T_START" \
    "test_voicestudio_with_no_url_cannot_validate_and_so_refuses_to_dispatch" \
    "test_the_default_wiring_refuses_a_voicestudio_voice_with_no_url"
restore

mp_mutation "another provider's voice is checked against the VoiceStudio list"
apply_mutation "$STARTER" '        if settings.voice_provider != "voicestudio":' '        if False:' \
  && expect_red "$T_START" \
    "test_another_providers_voice_is_not_checked_against_the_voicestudio_list" \
    "test_any_id_dispatches_for_a_provider_without_a_list"
restore

mp_mutation "a dead worker does not make the process exit with a failure"
apply_mutation "$RUN" '    return 1 if died else 0' '    return 0' \
  && expect_red "$T_SUPERVISE" "test_a_dead_worker_ends_the_process_with_a_failure_and_stops_the_rest"
restore

mp_mutation "activities get no grace period when a worker shuts down"
apply_mutation "$RUN" \
  '        graceful_shutdown_timeout=timedelta(seconds=GRACEFUL_SHUTDOWN_SECONDS),' \
  '        graceful_shutdown_timeout=timedelta(0),' \
  && expect_red "$T_SUPERVISE" "test_shutdown_lets_a_running_activity_finish"
restore

mp_mutation "the stems are uploaded without heartbeating"
apply_mutation "$INGEST" \
  '                    await run_blocking(self._put_stem, key, path)' \
  '                    await __import__("asyncio").to_thread(self._put_stem, key, path)' \
  && expect_red "$T_PREP" "test_ingest_heartbeats_while_the_stems_upload"
restore

mp_mutation "the Demucs timeout ignores the length of the media"
apply_mutation "$DEMUCS" \
  '        return max(self._timeout, TIMEOUT_PER_MEDIA_SECOND * duration_ms / 1000)' \
  '        return self._timeout' \
  && expect_red "$T_DEMUCS" \
    "test_the_timeout_grows_with_the_media_but_never_below_the_floor" \
    "test_a_long_video_is_given_the_time_its_length_needs"
restore

mp_mutation "cancelling an activity does not tell Demucs to stop"
apply_mutation "$FFMPEG" $'            cancel.set()\n' $'            pass\n' \
  && expect_red "$T_DEMUCS" "test_cancelling_the_activity_kills_the_demucs_process"
restore

mp_mutation "STT receives the 44.1 kHz stereo stem"
apply_mutation "$TRANSCRIBE" \
  '            audio = await run_blocking(_for_stt, stem)' '            audio = stem' \
  && expect_red "$T_PREP" "test_stt_gets_16khz_mono_not_the_44khz_stereo_stem"
restore

mp_mutation "the claim drops FOR UPDATE SKIP LOCKED"
apply_mutation "$STARTER" '                    LIMIT 1 FOR UPDATE SKIP LOCKED)' '                    LIMIT 1)' \
  && expect_red "$T_START" "test_two_starters_start_each_generation_exactly_once"
restore

mp_mutation "the throttle forgets what this process has just started"
apply_mutation "$STARTER" \
  '        known = self._recent[-1][1] if self._recent else 0' '        known = 0' \
  && expect_red "$T_START" "test_the_limit_holds_while_visibility_has_not_caught_up"
restore

mp_mutation "an unreachable Temporal counts against the row"
apply_mutation "$STARTER" '        return exc.status in SYSTEMIC_STATUSES' '        return False' \
  && expect_red "$T_START" "test_an_outage_never_kills_rows_and_is_not_counted_against_them"
restore

mp_mutation "persist_json accepts any key under work/"
apply_mutation "$PERSIST" '        if not PERSIST_KEY_RE.fullmatch(key):' '        if not key.startswith("work/"):' \
  && expect_red "$T_PREP" "test_persist_json_writes_under_work_and_refuses_anywhere_else"
restore

# --- second review: shutdown, sibling cancel, end of video, process group, grants ------------------
mp_mutation "workers are shut down one after another"
apply_mutation "$RUN" \
  '    await asyncio.gather(*(_shutdown(w) for w in workers))' \
  $'    for w in workers:\n        await _shutdown(w)' \
  && expect_red "$T_SUPERVISE" "test_workers_drain_at_the_same_time_not_one_after_another"
restore

mp_mutation "a failed stage leaves its sibling activity running"
apply_mutation "$WORKFLOW" $'            for handle, _ in stages.values():\n                if not handle.done():\n                    handle.cancel()' \
  $'            for handle, _ in stages.values():\n                if not handle.done():\n                    pass' \
  && expect_red "$T_PREP" "test_a_failed_stage_cancels_the_sibling_instead_of_leaving_it_running"
restore

mp_mutation "the end of a video is only recognised near the container's duration"
apply_mutation "$DETECT" '                        at_end=bool(boxes),' '                        at_end=False,' \
  && expect_red "$T_PREP" "test_a_container_longer_than_its_video_stream_does_not_fail_the_last_chunk"
restore

mp_mutation "only the demucs process is killed, not its process group"
apply_mutation "$DEMUCS" '                os.killpg(process.pid, signal.SIGKILL)' '                process.kill()' \
  && expect_red "$T_DEMUCS" "test_cancelling_the_activity_kills_the_demucs_process"
restore

mp_mutation "the release statement touches a column the starter has no grant on"
apply_mutation "$STARTER" 'SET last_error = %(error)s, next_attempt_at' \
  'SET generation = generation, last_error = %(error)s, next_attempt_at' \
  && expect_red "$T_GRANTS" "test_the_starter_can_run_every_statement_it_uses"
restore

# --- subtitle region padding and gap bridging (registered, not yet run) --------------------------
mp_mutation "a gap in time is never bridged"
apply_mutation "$DETECT" 'MAX_GAP_MS = 2000' 'MAX_GAP_MS = 0' \
  && expect_red "$T_PREP" \
    "test_the_real_clip_is_one_cue_and_its_region_covers_the_whole_text" \
    "test_a_gap_is_bridged_by_its_length_in_time_not_by_a_number_of_frames"
restore

mp_mutation "any gap is bridged"
apply_mutation "$DETECT" \
  '        if (index - last - 1) * FRAME_INTERVAL_MS > MAX_GAP_MS:' \
  '        if (index - last - 1) * FRAME_INTERVAL_MS > MAX_GAP_MS * 1000:' \
  && expect_red "$T_PREP" "test_a_gap_is_bridged_by_its_length_in_time_not_by_a_number_of_frames"
restore

mp_mutation "the detected region is not padded vertically"
apply_mutation "$DETECT" 'REGION_PAD_V = 0.30' 'REGION_PAD_V = 0.0' \
  && expect_red "$T_PREP" "test_the_real_clip_is_one_cue_and_its_region_covers_the_whole_text"
restore

mp_mutation "the padded region is not clamped to the frame"
apply_mutation "$DETECT" \
  '    left, top = max(0, region.x - dx), max(0, region.y - dy)' \
  '    left, top = region.x - dx, region.y - dy' \
  && expect_red "$T_PREP" "test_a_padded_region_stays_inside_the_frame_at_every_edge"
restore

# --- vision requests: answer only, capped (registered, not yet run) --------------------------------
mp_mutation "OCR requests let the model reason before answering"
apply_mutation "$VISION_OCR" \
  '_ANSWER_ONLY = {"reasoning": {"enabled": False}, "max_tokens": 200}' \
  '_ANSWER_ONLY = {"max_tokens": 200}' \
  && expect_red "$T_OCR" "test_every_ocr_request_asks_for_an_answer_without_reasoning_and_caps_it"
restore

mp_mutation "OCR requests have no useful token cap"
apply_mutation "$VISION_OCR" '"max_tokens": 200}' '"max_tokens": 20000}' \
  && expect_red "$T_OCR" "test_every_ocr_request_asks_for_an_answer_without_reasoning_and_caps_it"
restore

mp_mutation "a null reply content crashes instead of being the unparseable case"
apply_mutation "$VISION_OCR" 'TypeError, AttributeError, ValueError' 'TypeError, ValueError' \
  && expect_red "$T_OCR" "test_an_empty_or_cut_off_reply_is_the_unparseable_case_not_a_crash"
restore

# --- one malformed answer is not a failed provider (registered, not yet run) ---------------------
mp_mutation "a prose reply from the vision model is a failed provider, not an unusable answer"
apply_mutation "$VISION_OCR" \
  '            raise UnusableAnswer("ocr: the reply is not the requested JSON") from None' \
  '            raise PermanentProviderError("ocr: the reply is not the requested JSON") from None' \
  && expect_red "$T_OCR" "test_vision_ocr_prose_reply_is_an_unusable_answer_not_a_failed_provider"
restore

mp_mutation "the detector does not count the frames whose answer was unusable"
apply_mutation "$DETECT" $'            return None, True\n' $'            return None, False\n' \
  && expect_red "$T_PREP" "test_most_frames_unusable_fails_permanently_and_names_the_counts"
restore

mp_mutation "the detector tolerates any number of unusable answers"
apply_mutation "$DETECT" 'unusable * 2 > len(boxes)' 'unusable * 2 > len(boxes) * 100' \
  && expect_red "$T_PREP" "test_most_frames_unusable_fails_permanently_and_names_the_counts"
restore

mp_mutation "the reviewer's text check swallows every OCR failure, not only a malformed answer"
apply_mutation "$REVIEW_RENDER" '            except UnusableAnswer:' '            except Exception:' \
  && expect_red "$T_REVIEW" "test_an_ocr_that_is_broken_still_fails_the_review"
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
  $'        transcribing = workflow.start_activity(\n            "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n        )\n        detecting = workflow.start_activity(\n            "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n        )\n        await workflow.wait(' \
  $'        if workflow.patched("detect-before-transcribe"):\n            detecting = workflow.start_activity(\n                "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n            )\n            transcribing = workflow.start_activity(\n                "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n            )\n        else:\n            transcribing = workflow.start_activity(\n                "transcribe", args=[spec, ingest.vocals_key], result_type=Transcript, **TRANSCRIBE\n            )\n            detecting = workflow.start_activity(\n                "detect_subtitles", spec, result_type=SubtitleTrack, **DETECT_SUBTITLES\n            )\n        await workflow.wait(' \
  && expect_green "$T_REPLAY" "patched-reorder"
restore

mp_finish

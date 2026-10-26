#!/usr/bin/env bash
#
# Mutation suite for the dubbing stage:
#
#   - a TTS hash is sent to the provider at most once   (tests/activities, tests/workflows)
#     (the cache, the plan's de-duplication, what
#     makes up the hash, the write after the answer)
#   - the Dubber's speaking is limited by the tts queue (tests/workflows, tests/workers)
#   - what the Dubber may do: clamp, rewrites, ids      (tests/agents)
#   - synthesis and the track never reach an agent      (tests/architecture)
#   - how a line is fitted: the stretch table, the      (tests/activities)
#     overflow flag, the version after the Dubber,
#     a rewrite cannot add a contact
#   - the track: placement, stretch, ducking, loudness, (tests/activities)
#     the length of the video
#   - the workflow: a marker for runs recorded before   (tests/replay, tests/workflows)
#     the stage, one regeneration per lost stage
#
# A guard is evidence only if breaking its invariant turns it red, and the
# controls say WHICH mechanism carries it. Protocol and rules: see
# scripts/lib/mutation-protocol.sh.
#
# Run from anywhere:  bash services/localization/tests/mutations/phase-06.sh
# Needs `uv sync` in services/localization. Temporal runs in-process (the kill
# tests start their own worker processes and a local Temporal server).

set -u

cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=../../../../scripts/lib/mutation-protocol.sh
source ../../scripts/lib/mutation-protocol.sh

SYNTH=src/localization/activities/synthesize.py
DUBSCRIPT=src/localization/activities/dub_script.py
ASSEMBLE=src/localization/activities/assemble_audio.py
DUBPORTS=src/localization/activities/dubber_ports.py
DUBBER=src/localization/agents/dubber.py
TEMPORAL=src/localization/agents/temporal.py
WORKFLOW=src/localization/workflows/video_localization.py
RUN=src/localization/workers/run.py
STORE=src/localization/storage/object_store.py

T_SYNTH=tests/activities/test_synthesize.py
T_DUBSCRIPT=tests/activities/test_dub_script.py
T_ASM=tests/activities/test_assemble_audio.py
T_DUBBER=tests/agents/test_dubber.py
T_PORTS=tests/agents/test_dubber_ports.py
T_DEFERRED=tests/agents/test_deferred_model.py
T_STAGE=tests/workflows/test_dubbing_stage.py
T_RESUME=tests/workflows/test_resume_tts.py
T_WIRING=tests/workers/test_agent_wiring.py
T_REPLAY=tests/replay/test_replay.py
T_ARCH=tests/architecture/test_single_translation.py

mp_init "$SYNTH" "$DUBSCRIPT" "$ASSEMBLE" "$DUBPORTS" "$DUBBER" "$TEMPORAL" "$WORKFLOW" "$RUN" "$STORE"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)
MP_TIMEOUT_SECONDS=${MP_TIMEOUT_SECONDS:-300}

mp_baseline "$T_SYNTH $T_DUBSCRIPT $T_ASM $T_DUBBER $T_PORTS $T_DEFERRED $T_STAGE $T_RESUME $T_WIRING $T_REPLAY $T_ARCH"

# --- a hash is sent to the provider at most once ----------------------------------------------------------
mp_mutation "the cache is never read: every request goes to the provider"
apply_mutation "$SYNTH" $'        if known is not None:\n            return known' \
  $'        if False:\n            return known' \
  && expect_red "$T_SYNTH $T_RESUME" \
    "test_a_text_that_is_already_cached_is_not_sent_to_the_provider_again" \
    "test_a_worker_killed_after_two_batches_costs_no_repeated_call" \
    "test_a_worker_killed_between_the_answer_and_the_write_repeats_only_what_was_in_flight" \
    "test_a_second_run_over_the_same_script_makes_no_provider_call"
restore

mp_mutation "the plan lists a text once per segment instead of once"
apply_mutation "$SYNTH" \
  $'        hashes = segment_hashes(self._synth, spec.voice, engine, script)\n        return SynthesisPlan(hashes=tuple(hashes))' \
  $'        hashes = [self._synth.hash_of(spec.voice, engine, s.text_vi) for s in script.segments]\n        return SynthesisPlan(hashes=tuple(hashes))' \
  && expect_red "$T_SYNTH $T_RESUME" \
    "test_the_plan_lists_a_text_said_twice_once" \
    "test_a_text_said_twice_in_different_batches_is_sent_once"
restore

mp_mutation "the model is not part of the hash"
apply_mutation "$SYNTH" \
  $'                "model": model,\n                "engine": engine,\n                "voice_id": voice.voice_id,\n                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  $'                "engine": engine,\n                "voice_id": voice.voice_id,\n                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  && expect_red "$T_SYNTH $T_RESUME" \
    "test_every_part_of_how_a_clip_sounds_changes_its_hash" \
    "test_changing_the_model_or_the_engine_is_a_cache_miss" \
    "test_changing_the_model_between_runs_speaks_everything_again"
restore

mp_mutation "the engine is not part of the hash"
apply_mutation "$SYNTH" \
  $'                "model": model,\n                "engine": engine,\n                "voice_id": voice.voice_id,\n                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  $'                "model": model,\n                "voice_id": voice.voice_id,\n                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  && expect_red "$T_SYNTH $T_RESUME" \
    "test_every_part_of_how_a_clip_sounds_changes_its_hash" \
    "test_changing_the_model_or_the_engine_is_a_cache_miss" \
    "test_changing_the_engine_between_runs_speaks_everything_again"
restore

mp_mutation "the voice id is not part of the hash"
apply_mutation "$SYNTH" \
  $'                "engine": engine,\n                "voice_id": voice.voice_id,\n                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  $'                "engine": engine,\n                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  && expect_red "$T_SYNTH" "test_every_part_of_how_a_clip_sounds_changes_its_hash"
restore

mp_mutation "the voice settings are not part of the hash"
apply_mutation "$SYNTH" \
  $'                "voice_id": voice.voice_id,\n                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  $'                "voice_id": voice.voice_id,\n                "text_vi": text_vi,' \
  && expect_red "$T_SYNTH" "test_every_part_of_how_a_clip_sounds_changes_its_hash"
restore

mp_mutation "the text is not part of the hash"
apply_mutation "$SYNTH" \
  $'                "settings": voice.settings.model_dump(mode="json"),\n                "text_vi": text_vi,' \
  $'                "settings": voice.settings.model_dump(mode="json"),' \
  && expect_red "$T_SYNTH" "test_every_part_of_how_a_clip_sounds_changes_its_hash"
restore

mp_mutation "the before-put hook is never called, so the window cannot be probed"
apply_mutation "$SYNTH" '        if self._before_put is not None:' '        if False:' \
  && expect_red "$T_SYNTH" "test_the_object_is_not_there_when_the_before_put_hook_runs"
restore

mp_mutation "the clip is stored without the text it was made from"
apply_mutation "$SYNTH" '                META_TEXT: encoded,' '' \
  && expect_red "$T_SYNTH $T_PORTS" \
    "test_a_clip_is_stored_with_the_text_it_was_made_from" \
    "test_text_with_accents_and_emoji_survives_the_metadata_round_trip"
restore

mp_mutation "the object store drops the metadata it is given"
apply_mutation "$STORE" '            extra["Metadata"] = metadata' '            pass' \
  && expect_red "$T_SYNTH" "test_a_clip_is_stored_with_the_text_it_was_made_from"
restore

mp_mutation "the metadata names are renamed"
apply_mutation "$SYNTH" 'META_TEXT = "text-vi"' 'META_TEXT = "text_vi"' \
  && expect_red "$T_PORTS" "test_the_clip_metadata_names_are_the_ones_other_stages_read"
restore

mp_mutation "a hash no line has is looked up instead of refused"
apply_mutation "$SYNTH" '                if digest not in texts:' '                if False:' \
  && expect_red "$T_SYNTH" "test_a_hash_no_line_has_is_refused_without_calling_the_provider"
restore

mp_mutation "a batch does not heartbeat after each hash"
apply_mutation "$SYNTH" '                activity.heartbeat(digest)' '                pass' \
  && expect_red "$T_SYNTH" "test_a_batch_heartbeats_after_each_hash"
restore

# --- how a line is judged -----------------------------------------------------------------------------------
mp_mutation "a clip at exactly the stretch limit is treated as too long"
apply_mutation "$SYNTH" '    if ratio <= STRETCH_MAX:' '    if ratio < STRETCH_MAX:' \
  && expect_red "$T_SYNTH" "test_the_stretch_table"
restore

mp_mutation "a clip longer than its slot is never sped up"
apply_mutation "$SYNTH" $'    if ratio <= 1.0:\n        return 1.0' $'    if ratio <= STRETCH_MAX:\n        return 1.0' \
  && expect_red "$T_SYNTH" "test_the_stretch_table"
restore

mp_mutation "a line that still overflows is not marked"
apply_mutation "$SYNTH" \
  '    return min(max(wanted, STRETCH_MIN), STRETCH_MAX), needed > STRETCH_MAX' \
  '    return min(max(wanted, STRETCH_MIN), STRETCH_MAX), False' \
  && expect_red "$T_SYNTH $T_DUBSCRIPT" \
    "test_the_final_stretch_never_leaves_a_clip_longer_than_it_has_to_be" \
    "test_every_segment_is_in_the_document_with_a_stretch" \
    "test_a_line_still_too_long_is_marked_even_if_the_dubber_accepted_it"
restore

mp_mutation "the stretch the Dubber chose is ignored"
apply_mutation "$SYNTH" \
  '    wanted = max(1.0 if chosen is None else chosen, min(needed, STRETCH_MAX))' \
  '    wanted = max(1.0, min(needed, STRETCH_MAX))' \
  && expect_red "$T_SYNTH $T_DUBSCRIPT" \
    "test_the_final_stretch_never_leaves_a_clip_longer_than_it_has_to_be" \
    "test_the_stretch_the_dubber_chose_is_used_when_the_clip_fits_it"
restore

mp_mutation "every line is reported as fitting"
apply_mutation "$DUBSCRIPT" '            if stretch_for(duration, seg.slot_ms) is None:' '            if False:' \
  && expect_red "$T_DUBSCRIPT" "test_only_the_lines_that_cannot_fit_even_when_sped_up_are_reported"
restore

mp_mutation "the script after the Dubber keeps the version of the one before it"
apply_mutation "$DUBSCRIPT" \
  '"version": script.version + 1,' '"version": script.version,' \
  && expect_red "$T_DUBSCRIPT" \
    "test_the_dubbed_script_is_a_new_version_and_the_old_one_is_untouched" \
    "test_a_segment_the_dubber_left_alone_keeps_everything_but_the_version"
restore

mp_mutation "a rewrite that adds a contact is accepted"
apply_mutation "$DUBSCRIPT" \
  '            if contacts_missing_from_source(decision.text_vi, seg.src_zh):' \
  '            if False:' \
  && expect_red "$T_DUBSCRIPT" "test_a_rewrite_that_adds_a_link_is_refused_and_the_line_keeps_its_text"
restore

mp_mutation "decisions naming a segment twice or one that does not exist are applied"
apply_mutation "$DUBSCRIPT" \
  '    if len(chosen) != len(decisions.items) or not chosen.keys() <= by_id.keys():' \
  '    if False:' \
  && expect_red "$T_DUBSCRIPT" "test_decisions_that_do_not_fit_the_script_write_nothing"
restore

mp_mutation "the segments document leaves out the lines that need no stretch"
apply_mutation "$DUBSCRIPT" \
  $'        for seg in doc.segments:\n            clip = await' \
  $'        for seg in doc.segments[:1]:\n            clip = await' \
  && expect_red "$T_DUBSCRIPT" "test_every_segment_is_in_the_document_with_a_stretch"
restore

mp_mutation "a missing clip is skipped when measuring instead of failing"
apply_mutation "$DUBSCRIPT" \
  $'        clip = await self._synth.cached(voice, engine, text_vi)\n        if clip is None:\n            raise _missing_clip(self._synth.hash_of(voice, engine, text_vi))\n        return clip.duration_ms' \
  $'        clip = await self._synth.cached(voice, engine, text_vi)\n        if clip is None:\n            return 0\n        return clip.duration_ms' \
  && expect_red "$T_DUBSCRIPT" "test_a_line_whose_clip_is_missing_fails_instead_of_passing_as_fitting"
restore

# --- the Dubber ---------------------------------------------------------------------------------------------------
mp_mutation "the Dubber's tts tool is not pinned to the tts queue"
apply_mutation "$TEMPORAL" \
  '    **{name: {"temporal": TTS_TOOL_ACTIVITY} for name in dubber_module.TTS_TOOLS},' \
  '    **{name: {"temporal": TOOL_ACTIVITY} for name in dubber_module.TTS_TOOLS},' \
  && expect_red "$T_STAGE $T_WIRING" \
    "test_the_dubbers_tts_calls_never_exceed_the_tts_queue_limit" \
    "test_the_dubbers_tools_are_pinned_to_the_queues_that_limit_them" \
    "test_the_dubber_runs_for_the_lines_that_do_not_fit_and_its_decisions_are_applied"
restore

mp_mutation "a stretch outside the allowed range is accepted"
apply_mutation "$DUBBER" '    if not STRETCH_MIN <= stretch_ratio <= STRETCH_MAX:' '    if False:' \
  && expect_red "$T_DUBBER" "test_a_stretch_outside_the_allowed_range_is_refused"
restore

mp_mutation "a line may be rewritten without limit"
apply_mutation "$DUBBER" '        if spoken > MAX_REWRITES:' '        if spoken > MAX_REWRITES + 100:' \
  && expect_red "$T_DUBBER" \
    "test_a_line_is_spoken_at_most_three_times" \
    "test_the_fourth_rewrite_is_refused_with_the_way_out"
restore

mp_mutation "the output validator is gone: whatever the Dubber returns is accepted"
apply_mutation "$DUBBER" '        return check_output(ctx.deps, output)' '        return output' \
  && expect_red "$T_DUBBER" "test_the_agent_asks_again_until_every_line_has_a_decision"
restore

mp_mutation "a line that was not asked for can be spoken and accepted"
apply_mutation "$DUBBER" '    if segment_id not in deps.pending_ids:' '    if False:' \
  && expect_red "$T_DUBBER" \
    "test_a_segment_that_was_not_asked_for_cannot_be_accepted" \
    "test_a_line_that_was_not_asked_for_is_not_spoken"
restore

mp_mutation "video text reaches the Dubber outside an untrusted block"
apply_mutation "$DUBBER" \
  '        wrap_untrusted(f"[{seg.id}] source: {seg.src_zh}\nvietnamese: {seg.text_vi}")' \
  '        f"[{seg.id}] source: {seg.src_zh}\nvietnamese: {seg.text_vi}"' \
  && expect_red "$T_DUBBER" "test_a_line_is_shown_inside_an_untrusted_block_with_its_fit"
restore

mp_mutation "the Dubber's model request is answered by the Localizer's model"
apply_mutation "$TEMPORAL" '        factory = _model_factories.get(self._name)' '        factory = _model_factories.get(MODEL_ALIAS)' \
  && expect_red "$T_DEFERRED" "test_each_agent_gets_the_model_bound_under_its_own_alias"
restore

mp_mutation "reading a line is allowed to speak it when its clip is missing"
apply_mutation "$DUBPORTS" \
  '        clip = await self._synth.cached(deps.voice, deps.engine, seg.text_vi)' \
  '        clip = await self._synth.clip(deps.voice, deps.engine, seg.text_vi)' \
  && expect_red "$T_PORTS" "test_reading_a_line_never_speaks_even_when_its_clip_is_missing"
restore

# --- the stage never reaches an agent --------------------------------------------------------------------------------
mp_mutation "synthesis imports an agent"
apply_mutation "$SYNTH" '@append' $'\nfrom localization.agents import localizer  # noqa: E402,F401\n' \
  && expect_red "$T_ARCH" "test_stage_does_not_reach_an_agent"
restore

mp_mutation "the audio assembly imports an agent"
apply_mutation "$ASSEMBLE" '@append' $'\nfrom localization.agents import dubber  # noqa: E402,F401\n' \
  && expect_red "$T_ARCH" "test_stage_does_not_reach_an_agent"
restore

# --- the track ---------------------------------------------------------------------------------------------------------------
mp_mutation "clips are not delayed to their segment's start"
apply_mutation "$ASSEMBLE" 'adelay={start_ms}:all=1' 'adelay=0:all=1' \
  && expect_red "$T_ASM" \
    "test_the_graph_places_each_clip_at_its_start_and_stretch" \
    "test_a_clip_starts_where_its_segment_starts" \
    "test_clips_are_placed_independently_of_each_other"
restore

mp_mutation "clips are not played at their stretch ratio"
apply_mutation "$ASSEMBLE" 'atempo={ratio:.6f}' 'atempo=1.0' \
  && expect_red "$T_ASM" \
    "test_the_graph_places_each_clip_at_its_start_and_stretch" \
    "test_a_clip_is_played_at_its_stretch_ratio"
restore

mp_mutation "the music is not ducked under the voice"
apply_mutation "$ASSEMBLE" 'DUCK = "threshold=0.03:ratio=10:attack=20:release=300"' 'DUCK = "threshold=0.03:ratio=1:attack=20:release=300"' \
  && expect_red "$T_ASM" "test_the_music_is_ducked_while_the_voice_speaks"
restore

mp_mutation "the loudness is not normalised"
apply_mutation "$ASSEMBLE" '["-i", str(mixed), "-af", normalise, ' '["-i", str(mixed), "-af", "anull", ' \
  && expect_red "$T_ASM" "test_the_loudness_is_within_one_lu_of_the_goal"
restore

mp_mutation "the length of the track is not compared with the video"
apply_mutation "$ASSEMBLE" '    if abs(actual - duration_ms) > LENGTH_TOLERANCE_MS:' '    if False:' \
  && expect_red "$T_ASM" "test_a_voice_that_runs_past_the_end_of_the_video_fails_for_good"
restore

mp_mutation "a length mismatch is retried instead of being permanent"
apply_mutation "$ASSEMBLE" $'            type="AudioLengthMismatch",\n            non_retryable=True,' $'            type="AudioLengthMismatch",' \
  && expect_red "$T_ASM" "test_a_voice_that_runs_past_the_end_of_the_video_fails_for_good"
restore

mp_mutation "music shorter than the video is not padded"
apply_mutation "$ASSEMBLE" '[0:a]{fmt},apad=whole_dur={seconds:.3f},atrim=end={seconds:.3f}[bgm]' '[0:a]{fmt},atrim=end={seconds:.3f}[bgm]' \
  && expect_red "$T_ASM" \
    "test_the_graph_places_each_clip_at_its_start_and_stretch" \
    "test_music_shorter_than_the_video_is_padded_even_when_nobody_speaks"
restore

mp_mutation "segments made for another script version are used"
apply_mutation "$ASSEMBLE" '        if doc.script_version != script.version:' '        if False:' \
  && expect_red "$T_ASM" "test_segments_made_for_another_script_version_are_refused"
restore

mp_mutation "the round does not name the folder of the track"
apply_mutation "$ASSEMBLE" '        out_key = work_key(spec.aweme_id, round, "dubbed.wav")' '        out_key = work_key(spec.aweme_id, 0, "dubbed.wav")' \
  && expect_red "$T_ASM" "test_the_round_names_the_folder_the_track_is_written_to"
restore

# --- the workflow ---------------------------------------------------------------------------------------------------------------
mp_mutation "the dubbing stage has no marker: a run recorded before it breaks on replay"
apply_mutation "$WORKFLOW" '        if not workflow.patched(DUB_PATCH):' '        if False:' \
  && expect_red "$T_REPLAY" "test_every_recorded_history_still_replays"
restore

mp_mutation "the Dubber runs even when every line fits"
apply_mutation "$WORKFLOW" '        if fit.unfit_ids:' '        if True:' \
  && expect_red "$T_STAGE" "test_the_dubber_is_not_run_when_every_line_fits"
restore

mp_mutation "synthesis is sent in one batch of a hundred"
apply_mutation "$WORKFLOW" 'TTS_BATCH = 10' 'TTS_BATCH = 100' \
  && expect_red "$T_STAGE" "test_synthesis_is_sent_in_batches_of_ten_hashes_that_together_cover_the_plan"
restore

mp_mutation "a batch that failed leaves the other batches running"
apply_mutation "$WORKFLOW" \
  $'            for handle in handles:\n                if not handle.done():\n                    handle.cancel()' \
  $'            for handle in handles:\n                if not handle.done():\n                    pass' \
  && expect_red "$T_STAGE" "test_a_batch_that_fails_cancels_the_batches_still_waiting_before_the_stage_restarts"
restore

mp_mutation "a batch that failed is only noticed after the batches listed before it have finished"
apply_mutation "$WORKFLOW" \
  '            failed = next((h for h in handles if _failed(h)), None)' \
  '            failed = None' \
  && expect_red "$T_STAGE" "test_a_batch_that_fails_cancels_the_batches_still_waiting_before_the_stage_restarts"
restore

mp_mutation "the clips of the dubbed script are not made after the Dubber"
apply_mutation "$WORKFLOW" '        await self._synthesize(spec, engine, final.script_key)' '        pass' \
  && expect_red "$T_STAGE" "test_the_stage_runs_its_steps_in_order_each_on_its_own_queue"
restore

mp_mutation "the track is built from the script before the Dubber"
apply_mutation "$WORKFLOW" '            args=[spec, final, segments_key, prep.ingest, DUB_ROUND],' '            args=[spec, first, segments_key, prep.ingest, DUB_ROUND],' \
  && expect_red "$T_STAGE" "test_the_result_names_the_track_and_the_script_it_was_built_from"
restore

mp_mutation "a lost stage is regenerated again and again by the dubbing stage"
apply_mutation "$WORKFLOW" \
  '                if stage not in DUB_REGENERABLE_STAGES or stage in self._regenerated:' \
  '                if stage not in DUB_REGENERABLE_STAGES:' \
  && expect_red "$T_STAGE" \
    "test_a_second_loss_of_the_same_stage_fails_the_run" \
    "test_a_second_script_loss_fails_the_run"
restore

mp_mutation "a lost script is not regenerated by the dubbing stage"
apply_mutation "$WORKFLOW" 'DUB_REGENERABLE_STAGES = frozenset({"ingest", "localize", "dub_script"})' 'DUB_REGENERABLE_STAGES = frozenset({"ingest"})' \
  && expect_red "$T_STAGE" \
    "test_a_script_that_is_gone_is_written_again_by_the_localizer_and_dubbing_restarts" \
    "test_a_dubbed_script_that_is_gone_restarts_the_dubbing_step_only"
restore

# --- the workers -------------------------------------------------------------------------------------------------------------------
mp_mutation "the tts queue's worker does not serve the Dubber's tool"
apply_mutation "$RUN" $'                activities=[voice_rate.voice_rate, synthesis.synthesize_segments],\n                plugins=agents,' $'                activities=[voice_rate.voice_rate, synthesis.synthesize_segments],' \
  && expect_red "$T_WIRING" "test_the_dubbing_stage_is_served_on_the_queues_the_workflow_sends_it_to"
restore

mp_mutation "the worker does not bind the Dubber's ports"
apply_mutation "$RUN" '        dubber_module.bind_ports(StoreDubberPorts(store, synthesizer))' '        pass' \
  && expect_red "$T_WIRING" "test_a_configured_worker_has_its_ports_and_models_bound"
restore

mp_mutation "the media worker ignores the configured loudness"
apply_mutation "$RUN" '            loudness_lufs=settings.loudness_lufs,' '            loudness_lufs=-14.0,' \
  && expect_red "$T_WIRING" "test_the_media_worker_normalises_to_the_configured_loudness"
restore

# --- controls: changes that must NOT matter ------------------------------------------------------------------------------------------
mp_mutation "CONTROL: the Dubber's instructions are reworded (no command changes, so every history still replays)"
apply_mutation "$DUBBER" \
  'You tighten the Vietnamese dubbing lines of a short video so each is spoken within the' \
  'You shorten the Vietnamese dubbing lines of a short video so each is spoken within the' \
  && expect_green "$T_REPLAY" "instructions-reworded"
restore

mp_mutation "CONTROL: the music is ducked a little less hard"
apply_mutation "$ASSEMBLE" 'DUCK = "threshold=0.03:ratio=10:attack=20:release=300"' 'DUCK = "threshold=0.03:ratio=8:attack=20:release=300"' \
  && expect_green "$T_ASM" "duck-ratio"
restore

mp_mutation "CONTROL: a background heartbeat every three seconds instead of five"
apply_mutation "$SYNTH" 'BACKGROUND_HEARTBEAT_SECONDS = 5.0' 'BACKGROUND_HEARTBEAT_SECONDS = 3.0' \
  && expect_green "$T_SYNTH" "heartbeat-interval"
restore

mp_finish

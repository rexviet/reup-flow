#!/usr/bin/env bash
#
# Mutation suite for the review, publish and rerun steps:
#
#   - which stage a failed review sends the run back to      (tests/workflows)
#     (and which it does NOT run again), what is published
#     and what goes to a person, what a stage that fails
#     for good leaves on the run
#   - the folders of a later round, the next script version  (tests/activities, tests/agents)
#   - the characters a caption cannot show, refused early    (tests/text, tests/agents, tests/activities)
#   - the Reviewer: frame limits, no frames in a span,       (tests/agents, tests/activities)
#     untrusted text, the shape of a verdict, the local copy
#   - the worker serves each step on the queue the workflow  (tests/workers)
#     sends it to
#   - the marker for runs recorded before these steps        (tests/replay)
#
# A guard is evidence only if breaking its invariant turns it red, and the controls say
# WHICH mechanism carries it. Protocol and rules: scripts/lib/mutation-protocol.sh.
#
# Run from anywhere:  bash services/localization/tests/mutations/phase-08.sh
# Needs `uv sync` in services/localization, and a Postgres on localhost:5432 for the
# publish tests that read a real `localization.runs` row (CI fails without it; locally
# those tests skip). Temporal runs in-process, from a local dev server.
#
# GENERATED-LOOKING ON PURPOSE: each mutation names the exact tests that must go red.
# When a test or a line of source it mutates is renamed, the harness says
# "mutation matched nothing" and stops; it never passes quietly.

set -u

cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=../../../../scripts/lib/mutation-protocol.sh
source ../../scripts/lib/mutation-protocol.sh

WORKFLOW=src/localization/workflows/video_localization.py
ROUTING=src/localization/workflows/review_routing.py
PUBLISH=src/localization/activities/publish.py
REVIEWER=src/localization/agents/reviewer.py
REVIEWACT=src/localization/activities/review_render.py
GLYPHS=src/localization/text/glyphs.py
LOCALIZER=src/localization/agents/localizer.py
DUBSCRIPT=src/localization/activities/dub_script.py
ASSEMBLESCRIPT=src/localization/activities/assemble_script.py
SCRIPTDOC=src/localization/activities/script_document.py
RUN=src/localization/workers/run.py
TEMPORAL=src/localization/agents/temporal.py

T_LOOP=tests/workflows/test_review_loop.py
T_ONE=tests/workflows/test_one_translation.py
T_ROUTE=tests/workflows/test_review_routing.py
T_PAYLOAD=tests/workflows/test_review_payloads.py
T_PUBLISH=tests/activities/test_publish.py
T_REVIEWER=tests/agents/test_reviewer.py
T_REVIEWACT=tests/activities/test_review_render.py
T_GLYPHS=tests/text/test_glyphs.py
T_LOCALIZER=tests/agents/test_localizer.py
T_DUBSCRIPT=tests/activities/test_dub_script.py
T_ASSEMBLESCRIPT=tests/agents/test_assemble_script.py
T_WIRING=tests/workers/test_agent_wiring.py
T_REPLAY=tests/replay/test_replay.py
T_DUBSTAGE=tests/workflows/test_dubbing_stage.py

mp_init "$WORKFLOW" "$ROUTING" "$PUBLISH" "$REVIEWER" "$REVIEWACT" "$GLYPHS" "$LOCALIZER" "$DUBSCRIPT" "$ASSEMBLESCRIPT" "$SCRIPTDOC" "$RUN" "$TEMPORAL"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)
MP_TIMEOUT_SECONDS=${MP_TIMEOUT_SECONDS:-400}

mp_baseline "$T_LOOP $T_ONE $T_ROUTE $T_PAYLOAD $T_PUBLISH $T_REVIEWER $T_REVIEWACT $T_GLYPHS $T_LOCALIZER $T_DUBSCRIPT $T_ASSEMBLESCRIPT $T_WIRING $T_REPLAY $T_DUBSTAGE"

# --- which stage a failed review sends the run back to ---------------------------------------------------
mp_mutation "a failed review of one line runs the Localizer and everything after it again"
apply_mutation "$WORKFLOW" $'        if route.rerun == "localize":' \
  $'        if route.rerun in ("localize", "dub"):' \
  && expect_red "$T_LOOP" "test_a_line_read_badly_runs_only_the_dubber_for_that_line_and_what_follows"
restore

mp_mutation "the captions are laid out from the track before the rerun"
apply_mutation "$WORKFLOW" $'            script, dub = await self._rerun(spec, prep, script, dub, route, round_no)' \
  $'            script, _ = await self._rerun(spec, prep, script, dub, route, round_no)' \
  && expect_red "$T_LOOP" "test_a_line_read_badly_runs_only_the_dubber_for_that_line_and_what_follows" "test_a_wrong_translation_runs_the_localizer_again_with_the_notes_and_what_follows"
restore

mp_mutation "the layout after a rerun is made from the track before it (real layout, real clips)"
apply_mutation "$WORKFLOW" $'            script, dub = await self._rerun(spec, prep, script, dub, route, round_no)' \
  $'            script, _ = await self._rerun(spec, prep, script, dub, route, round_no)' \
  && expect_red "$T_ONE" "test_after_a_rerun_the_captions_are_the_text_that_was_spoken"
restore

mp_mutation "the layout after a rerun writes into the first pass's folder"
apply_mutation "$WORKFLOW" $'                args=[spec, final, prep.subtitles, prep.ingest, dub, round_no],' \
  $'                args=[spec, final, prep.subtitles, prep.ingest, dub, 0],' \
  && expect_red "$T_LOOP" "test_a_line_read_badly_runs_only_the_dubber_for_that_line_and_what_follows"
restore

mp_mutation "the captions are laid out from the Localizer's script, not the one the track was built from"
apply_mutation "$WORKFLOW" $'            final = LocalizedScriptRef(script_key=dub.script_key, version=dub.script_version)\n            spec_ref' \
  $'            final = LocalizedScriptRef(\n                script_key=script.script.script_key, version=script.script.version\n            )\n            spec_ref' \
  && expect_red "$T_LOOP" "test_the_captions_and_the_review_use_the_script_the_track_was_built_from" "test_a_line_read_badly_runs_only_the_dubber_for_that_line_and_what_follows"
restore

mp_mutation "the Localizer is run again without the reviewer's notes"
apply_mutation "$WORKFLOW" $'                ),\n                route.notes,\n            )\n            fresh = await' \
  $'                ),\n            )\n            fresh = await' \
  && expect_red "$T_LOOP" "test_a_wrong_translation_runs_the_localizer_again_with_the_notes_and_what_follows"
restore

mp_mutation "a translation made again is not the next version"
apply_mutation "$WORKFLOW" $'                    dub.script_version + 1,\n' \
  $'                    1,\n' \
  && expect_red "$T_LOOP" "test_a_wrong_translation_runs_the_localizer_again_with_the_notes_and_what_follows"
restore

mp_mutation "the lines the Dubber decided before lose their decisions on a rerun"
apply_mutation "$WORKFLOW" $'        self._decisions = {**self._decisions, **{d.id: d for d in decisions.items}}' \
  $'        self._decisions = {d.id: d for d in decisions.items}' \
  && expect_red "$T_LOOP" "test_the_lines_the_dubber_decided_before_keep_their_decisions"
restore

mp_mutation "the run is sent round a third time"
apply_mutation "$WORKFLOW" $'REVIEW_ROUNDS = 2' \
  $'REVIEW_ROUNDS = 3' \
  && expect_red "$T_LOOP" "test_a_run_that_fails_every_review_is_kept_for_a_person_and_not_published"
restore

mp_mutation "a layout that came out the same is reviewed again"
apply_mutation "$WORKFLOW" $'            if attempt and spec_ref.spec_sha256 == earlier_spec:' \
  $'            if False:' \
  && expect_red "$T_LOOP" "test_a_layout_that_comes_out_the_same_is_not_reviewed_twice"
restore

mp_mutation "the first translation is assembled with a shorter argument list"
apply_mutation "$WORKFLOW" $'            args=[spec, transcript_key, subtitle_key, lines, 0, 1],' \
  $'            args=[spec, transcript_key, subtitle_key, lines],' \
  && expect_red "$T_LOOP" "test_a_run_that_passes_review_is_published_to_output"
restore

mp_mutation "the review steps run for a run recorded before them"
apply_mutation "$WORKFLOW" $'        if not workflow.patched(REVIEW_PATCH):\n            return LocalizationResult(prep=prep, script=script, dub=dub)\n' \
  $'' \
  && expect_red "$T_REPLAY" "test_every_recorded_history_still_replays"
restore

# --- what is published and what goes to a person ---------------------------------------------------------
mp_mutation "a run that needs a person is published anyway"
apply_mutation "$WORKFLOW" $'        ok = verdict is not None and verdict.passed and not reasons' \
  $'        ok = verdict is not None and verdict.passed' \
  && expect_red "$T_LOOP" "test_a_flagged_translation_is_never_published_even_when_the_review_passes" "test_a_refused_dubber_rewrite_is_never_published"
restore

mp_mutation "a run that failed review twice is published"
apply_mutation "$WORKFLOW" $'                    outcome="published" if ok else "needs_human",' \
  $'                    outcome="published",' \
  && expect_red "$T_LOOP" "test_a_run_that_fails_every_review_is_kept_for_a_person_and_not_published"
restore

mp_mutation "a flagged run is sent round again before a person sees it"
apply_mutation "$WORKFLOW" $'            if verdict.passed or flags:' \
  $'            if verdict.passed:' \
  && expect_red "$T_LOOP" "test_a_flagged_run_is_not_sent_round_again_it_goes_to_the_person_with_the_verdict"
restore

mp_mutation "a refused rewrite is forgotten"
apply_mutation "$DUBSCRIPT" $'            script_key=written.script_key, version=written.version, refused_ids=tuple(refused)' \
  $'            script_key=written.script_key, version=written.version, refused_ids=()' \
  && expect_red "$T_DUBSCRIPT" "test_a_rewrite_with_a_contact_is_reported_even_next_to_one_with_a_glyph"
restore

mp_mutation "the publish activity writes output/ for a render that did not pass"
apply_mutation "$PUBLISH" $'                if request.verdict is None or not request.verdict.passed or request.reasons:' \
  $'                if False:' \
  && expect_red "$T_PUBLISH" "test_nothing_that_did_not_pass_is_ever_written_to_output"
restore

mp_mutation "a run for a person is copied to output/ as well"
apply_mutation "$PUBLISH" $'                review = review_key(spec.aweme_id, spec.generation)\n' \
  $'                review = review_key(spec.aweme_id, spec.generation)\n                output = output_key(spec.aweme_id)\n                await asyncio.to_thread(\n                    self._store.copy, request.render_key, output, produced_by="render"\n                )\n' \
  && expect_red "$T_PUBLISH" "test_a_run_for_a_person_keeps_its_render_under_review_by_generation"
restore

mp_mutation "the kept render does not carry the generation"
apply_mutation "$PUBLISH" $'                review = review_key(spec.aweme_id, spec.generation)\n' \
  $'                review = review_key(spec.aweme_id, 0)\n' \
  && expect_red "$T_PUBLISH" "test_the_render_of_each_generation_is_kept_apart" "test_a_run_for_a_person_keeps_its_render_under_review_by_generation"
restore

mp_mutation "a failed run keeps a render"
apply_mutation "$PUBLISH" $'            if request.render_key is not None:\n                raise _refuse("a failed run has no render to keep")' \
  $'            if False:\n                raise _refuse("a failed run has no render to keep")' \
  && expect_red "$T_PUBLISH" "test_a_failed_run_has_no_render_to_keep"
restore

mp_mutation "a later outcome of the same generation does not replace the row"
apply_mutation "$PUBLISH" $'ON CONFLICT (aweme_id, generation) DO UPDATE SET\n  workflow_id = EXCLUDED.workflow_id,\n  status = EXCLUDED.status,\n  output_key = EXCLUDED.output_key,\n  review_key = EXCLUDED.review_key,\n  verdict = EXCLUDED.verdict,\n  review_rounds = EXCLUDED.review_rounds,\n  needs_human = EXCLUDED.needs_human,\n  updated_at = now()' \
  $'ON CONFLICT (aweme_id, generation) DO NOTHING' \
  && expect_red "$T_PUBLISH" "test_a_later_outcome_of_the_same_generation_replaces_the_row"
restore

# --- a stage that fails for good -------------------------------------------------------------------------
mp_mutation "a failure is not written on the run"
apply_mutation "$WORKFLOW" $'            await self._record_failure(spec, error)\n            raise' \
  $'            raise' \
  && expect_red "$T_LOOP" "test_a_permanent_failure_ends_the_run_with_its_reason_on_it_and_nothing_published"
restore

mp_mutation "a failure that cannot be written replaces the one that was being reported"
apply_mutation "$WORKFLOW" $'        except ActivityError:\n            # The run is failing for the reason above; not being able to say so must not\n            # replace it.\n            pass' \
  $'        except ActivityError:\n            raise' \
  && expect_red "$T_LOOP" "test_a_failure_that_cannot_be_written_on_the_run_does_not_hide_the_failure"
restore

mp_mutation "an agent that gives up during a rerun is not written on the run"
apply_mutation "$WORKFLOW" $'        except (ActivityError, ApplicationError) as error:' \
  $'        except ActivityError as error:' \
  && expect_red "$T_LOOP" "test_an_agent_that_gives_up_during_a_rerun_is_written_on_the_run_too"
restore

mp_mutation "an agent that gives up raises an error the workflow cannot name"
apply_mutation "$TEMPORAL" $'        raise _gave_up("Localizer", exc) from None' \
  $'        raise' \
  && expect_red "$T_LOOP" "test_an_agent_that_gives_up_during_a_rerun_is_written_on_the_run_too"
restore

# --- the routing, as a function --------------------------------------------------------------------------
mp_mutation "a translation problem does not win over the stages after it"
apply_mutation "$ROUTING" $'    if "localize" in stages:' \
  $'    if False:' \
  && expect_red "$T_ROUTE" "test_a_translation_problem_wins_over_everything_after_it"
restore

mp_mutation "a reading problem with no line named is routed anyway"
apply_mutation "$ROUTING" $'        if any(i.segment_id is None for i in dub):' \
  $'        if False:' \
  && expect_red "$T_ROUTE" "test_a_reading_problem_with_no_line_named_has_no_route"
restore

mp_mutation "a broken picture is sent to the layout"
apply_mutation "$ROUTING" $'    if "captions" in stages:' \
  $'    if True:' \
  && expect_red "$T_ROUTE" "test_problems_only_in_stages_a_rerun_cannot_fix_have_no_route"
restore

# --- the folders of a later round ------------------------------------------------------------------------
mp_mutation "a script made after a review goes into the first pass's folder"
apply_mutation "$SCRIPTDOC" $'    return work_key(aweme_id, round, f"script.v{version}.json")' \
  $'    return work_key(aweme_id, 0, f"script.v{version}.json")' \
  && expect_red "$T_DUBSCRIPT" "test_a_dubbed_script_made_after_a_review_goes_into_that_rounds_folder"
restore

mp_mutation "the dub document of a later round overwrites the first pass's"
apply_mutation "$DUBSCRIPT" $'        key = work_key(spec.aweme_id, round, "dub-segments.json")' \
  $'        key = work_key(spec.aweme_id, 0, "dub-segments.json")' \
  && expect_red "$T_DUBSCRIPT" "test_the_dub_document_of_a_later_round_is_written_into_its_folder"
restore

mp_mutation "a translation made again overwrites the first one"
apply_mutation "$ASSEMBLESCRIPT" $'        key = work_key(spec.aweme_id, round, f"script.v{version}.json")' \
  $'        key = work_key(spec.aweme_id, 0, f"script.v{version}.json")' \
  && expect_red "$T_ASSEMBLESCRIPT" "test_a_translation_made_again_after_a_review_is_the_next_version_in_its_round"
restore

# --- the characters a caption cannot show ----------------------------------------------------------------
mp_mutation "the Localizer's answer is not checked for characters the font lacks"
apply_mutation "$LOCALIZER" $'    foreign = {i: chars for i, chars in sorted(foreign.items()) if chars}' \
  $'    foreign = {}' \
  && expect_red "$T_LOCALIZER" "test_a_line_with_a_character_the_caption_font_lacks_is_sent_back_naming_it"
restore

mp_mutation "a Dubber rewrite with such a character is accepted"
apply_mutation "$DUBSCRIPT" $'            elif foreign_chars(decision.text_vi):' \
  $'            elif False:' \
  && expect_red "$T_DUBSCRIPT" "test_a_rewrite_with_a_character_the_caption_font_lacks_is_not_used_and_not_reported"
restore

mp_mutation "letters of other scripts pass"
apply_mutation "$GLYPHS" $'            if not _is_latin_letter(char):' \
  $'            if False:' \
  && expect_red "$T_GLYPHS" "test_text_the_font_cannot_draw_is_flagged_in_order_without_repeats"
restore

mp_mutation "full-width and CJK punctuation passes"
apply_mutation "$GLYPHS" $'        if code in _CJK_PUNCTUATION or code in _HALF_AND_FULLWIDTH:' \
  $'        if False:' \
  && expect_red "$T_GLYPHS" "test_text_the_font_cannot_draw_is_flagged_in_order_without_repeats"
restore

mp_mutation "emoji and pictographs pass"
apply_mutation "$GLYPHS" $'        elif category in ("So", "Sk") or category in ("Cs", "Co", "Cn"):' \
  $'        elif False:' \
  && expect_red "$T_GLYPHS" "test_text_the_font_cannot_draw_is_flagged_in_order_without_repeats"
restore

mp_mutation "the allowlist loses to the category rules (a degree sign is refused)"
apply_mutation "$GLYPHS" $'        if code < 128 or char in _ALLOWED_SYMBOLS:' \
  $'        if code < 128:' \
  && expect_red "$T_GLYPHS" "test_the_degree_sign_the_percent_sign_and_the_minus_sign_are_not_flagged"
restore

mp_mutation "a rewrite refused only for a glyph flags the run"
apply_mutation "$DUBSCRIPT" $'            elif foreign_chars(decision.text_vi):' \
  $'            elif foreign_chars(decision.text_vi) and refused.append(seg.id) is None:' \
  && expect_red "$T_DUBSCRIPT" "test_a_rewrite_with_a_character_the_caption_font_lacks_is_not_used_and_not_reported"
restore

mp_mutation "the caption itself is reported as left-over subtitle"
apply_mutation "$REVIEWER" $'    places = [deps.spec.caption_box]' \
  $'    places = []' \
  && expect_red "$T_REVIEWER" "test_without_a_cover_the_caption_itself_is_not_reported_as_residue" "test_a_caption_box_taller_than_the_cover_is_still_the_caption"
restore

mp_mutation "the residue check has no call cap"
apply_mutation "$REVIEWER" $'    if deps.residual_calls >= MAX_RESIDUAL_CALLS:' \
  $'    if False:' \
  && expect_red "$T_REVIEWER" "test_a_fourth_call_to_look_for_residue_is_refused_and_costs_nothing"
restore

mp_mutation "an OCR that cannot read a frame ends the review"
apply_mutation "$REVIEWACT" $'            except PermanentProviderError:' \
  $'            except KeyError:' \
  && expect_red "$T_REVIEWACT" "test_an_ocr_that_cannot_read_a_frame_does_not_end_the_review"
restore

mp_mutation "a review that cannot be made ends the run as failed"
apply_mutation "$WORKFLOW" $'            except ActivityError as error:\n                # The video was made and nobody could judge it' \
  $'            except KeyError as error:\n                # The video was made and nobody could judge it' \
  && expect_red "$T_LOOP" "test_a_review_that_cannot_be_made_keeps_the_render_for_a_person"
restore

mp_mutation "a failure before the layout is not written on the run"
apply_mutation "$WORKFLOW" $'            if workflow.patched(REVIEW_PATCH):\n                await self._record_failure(spec, error)' \
  $'            if False:\n                await self._record_failure(spec, error)' \
  && expect_red "$T_LOOP" "test_a_localizer_that_gives_up_on_the_first_pass_is_written_on_the_run" "test_a_permanent_speech_error_is_written_on_the_run"
restore

# --- the Reviewer and its activity -----------------------------------------------------------------------
mp_mutation "more than eight frames in one call"
apply_mutation "$REVIEWER" $'    if len(times_ms) > MAX_FRAMES_PER_CALL:' \
  $'    if False:' \
  && expect_red "$T_REVIEWER" "test_more_than_eight_frames_in_one_call_is_refused"
restore

mp_mutation "a fourth call for frames"
apply_mutation "$REVIEWER" $'    if deps.frame_calls >= MAX_FRAME_CALLS:' \
  $'    if False:' \
  && expect_red "$T_REVIEWER" "test_a_fourth_call_for_frames_is_refused_and_costs_nothing" "test_the_frame_limit_holds_through_the_agent"
restore

mp_mutation "frames in the telemetry"
apply_mutation "$REVIEWER" $'                    include_binary_content=False, tracer_provider=tracer_provider' \
  $'                    include_binary_content=True, tracer_provider=tracer_provider' \
  && expect_red "$T_REVIEWER" "test_the_agent_does_not_put_frames_in_its_telemetry"
restore

mp_mutation "the script reaches the model outside an untrusted block"
apply_mutation "$REVIEWER" $'        for s in ctx.deps.script.segments\n    ]\n    return wrap_untrusted("\\n".join(rows))' \
  $'        for s in ctx.deps.script.segments\n    ]\n    return "\\n".join(rows)' \
  && expect_red "$T_REVIEWER" "test_the_script_and_the_residual_text_reach_the_model_as_data"
restore

mp_mutation "a failing verdict with no issue is accepted"
apply_mutation "$REVIEWER" $'    if not verdict.passed and not verdict.issues:' \
  $'    if False:' \
  && expect_red "$T_REVIEWER" "test_a_failing_verdict_without_an_issue_is_refused"
restore

mp_mutation "an issue about a segment that does not exist is accepted"
apply_mutation "$REVIEWER" $'    if unknown:' \
  $'    if False:' \
  && expect_red "$T_REVIEWER" "test_an_issue_about_a_segment_that_does_not_exist_is_refused"
restore

mp_mutation "a wide video is not scaled down for the model"
apply_mutation "$REVIEWACT" $'                f"scale={width}:-2",' \
  $'                "scale=iw:-2",' \
  && expect_red "$T_REVIEWACT" "test_a_wide_video_is_scaled_down_to_the_frame_width"
restore

mp_mutation "the local copy of the video is kept"
apply_mutation "$REVIEWACT" $'            shutil.rmtree(root, ignore_errors=True)' \
  $'            pass' \
  && expect_red "$T_REVIEWACT" "test_the_local_copy_of_the_video_is_removed_when_the_review_ends"
restore

mp_mutation "a reviewer that never settles is not stopped"
apply_mutation "$REVIEWACT" $'                    usage_limits=USAGE,\n' \
  $'' \
  && expect_red "$T_REVIEWACT" "test_a_reviewer_that_never_settles_fails_for_good"
restore

# --- the worker ------------------------------------------------------------------------------------------
mp_mutation "the publish activity is not served"
apply_mutation "$RUN" $'                    dub.commit_dub,\n                    publish.publish,' \
  $'                    dub.commit_dub,' \
  && expect_red "$T_WIRING" "test_the_review_and_the_publish_are_served_on_the_queues_the_workflow_sends_them_to"
restore

mp_mutation "the review activity is not served"
apply_mutation "$RUN" $'                "llm", settings.llm_concurrency, activities=[review.review_render], plugins=agents' \
  $'                "llm", settings.llm_concurrency, activities=[], plugins=agents' \
  && expect_red "$T_WIRING" "test_the_review_and_the_publish_are_served_on_the_queues_the_workflow_sends_them_to"
restore

mp_mutation "a worker starts without a reviewer model"
apply_mutation "$RUN" $'    model_for("reviewer", settings)\n' \
  $'' \
  && expect_red "$T_WIRING" "test_the_agents_worker_refuses_to_start_without_a_reviewer_model"
restore

# --- controls: changes that must NOT matter --------------------------------------------------------------
mp_mutation "CONTROL: the publish activity's own check is removed, and the workflow still keeps a person's runs out of output/"
apply_mutation "$PUBLISH" $'                if request.verdict is None or not request.verdict.passed or request.reasons:' \
  $'                if False:' \
  && expect_green "$T_LOOP" "workflow-decides"
restore

mp_mutation "CONTROL: modifier symbols are not looked for (no test uses one)"
apply_mutation "$GLYPHS" $'        elif category in ("So", "Sk") or category in ("Cs", "Co", "Cn"):' \
  $'        elif category == "So" or category in ("Cs", "Co", "Cn"):' \
  && expect_green "$T_GLYPHS" "modifier-symbols"
restore

mp_finish

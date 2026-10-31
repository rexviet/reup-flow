#!/usr/bin/env bash
#
# Mutation suite for the model-cost recording (registered, not yet run):
#
#   - a generation's cost, tokens and model reach the span   (tests/observability)
#     in the form Langfuse ingests
#   - a cost that is not a sane number is never recorded
#   - one row per OpenRouter generation id, best effort
#   - the run's total is the sum over its own video and generation
#   - the agents' models and OCR share the one priced client
#   - a finished run's scores reach its own trace, once each,  (tests/observability)
#     never failing the run, in the form Langfuse accepts
#   - one run is one trace holding every step
#   - a provider span names provider, model and input size, and no content
#   - the workflow tells publish which track to score and whether a line was flagged
#   - every sampled frame's OCR answer is stored with the subtitle region
#
# Protocol and rules: see scripts/lib/mutation-protocol.sh.
#
# Run from anywhere:  bash services/localization/tests/mutations/phase-09.sh
# Needs `uv sync` in services/localization and the compose Postgres on localhost:5432.

set -u

cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=../../../../scripts/lib/mutation-protocol.sh
source ../../scripts/lib/mutation-protocol.sh

USAGE=src/localization/observability/llm_usage.py
GENERATION=src/localization/observability/generation.py
CALLS=src/localization/observability/llm_calls.py
LLM=src/localization/providers/llm.py
REGISTRY=src/localization/providers/registry.py
PUBLISH=src/localization/activities/publish.py
SCORES=src/localization/observability/scores.py
SPANS=src/localization/observability/provider_spans.py
HTTP_BASE=src/localization/providers/base.py
CLIENT=src/localization/temporal/client.py
WORKFLOW=src/localization/workflows/video_localization.py
DETECT=src/localization/activities/detect_subtitles.py
SINGLE=src/localization/agents/localizer_single.py

T_COST=tests/observability/test_llm_cost.py
T_SCORES=tests/observability/test_run_scores.py
T_TRACE=tests/observability/test_trace_completeness.py
T_SPANS=tests/observability/test_provider_spans.py
T_LOOP=tests/workflows/test_review_loop.py
T_PREP=tests/workflows/test_prep_stage.py
T_ONE=tests/agents/test_one_request_agents.py

mp_init "$USAGE" "$GENERATION" "$CALLS" "$LLM" "$REGISTRY" "$PUBLISH" "$SCORES" "$SPANS" "$HTTP_BASE" \
  "$CLIENT" "$WORKFLOW" "$DETECT" "$SINGLE"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)

mp_baseline "$T_COST $T_SCORES $T_TRACE $T_SPANS $T_LOOP $T_PREP $T_ONE"

mp_mutation "the span is not marked as a generation, so Langfuse drops its cost"
apply_mutation "$GENERATION" '    attributes = {OBSERVATION_TYPE: "generation"}' '    attributes = {}' \
  && expect_red "$T_COST" "test_the_cost_reaches_the_span_in_the_form_langfuse_ingests"
restore

mp_mutation "the span carries a cost of zero instead of the reported one"
apply_mutation "$GENERATION" '"total": usage.cost_usd})' '"total": 0.0})' \
  && expect_red "$T_COST" "test_the_cost_reaches_the_span_in_the_form_langfuse_ingests"
restore

mp_mutation "a negative or non-finite cost is accepted"
apply_mutation "$USAGE" \
  '    return float(value) if math.isfinite(value) and value >= 0 else None' \
  '    return float(value)' \
  && expect_red "$T_COST" "test_a_cost_that_is_not_a_sane_number_is_none_never_made_up"
restore

mp_mutation "a boolean counts as a cost"
apply_mutation "$USAGE" \
  '    if isinstance(value, bool) or not isinstance(value, int | float):' \
  '    if not isinstance(value, int | float):' \
  && expect_red "$T_COST" "test_a_cost_that_is_not_a_sane_number_is_none_never_made_up"
restore

mp_mutation "the same response id adds to its cost each time it is recorded"
apply_mutation "$CALLS" \
  'ON CONFLICT (generation_id) DO NOTHING' \
  'ON CONFLICT (generation_id) DO UPDATE SET cost_usd = EXCLUDED.cost_usd + llm_calls.cost_usd' \
  && expect_red "$T_COST" "test_the_same_response_id_is_counted_once"
restore

mp_mutation "a failed write is not logged as a recorded-call failure"
apply_mutation "$CALLS" '        except Exception as exc:  # noqa: BLE001' '        except ZeroDivisionError as exc:' \
  && expect_red "$T_COST" "test_a_database_that_is_down_does_not_fail_the_request_and_the_log_has_no_password"
restore

mp_mutation "the run's total sums the video's other generations too"
apply_mutation "$PUBLISH" \
  'WHERE aweme_id = %s AND generation = %s),' \
  'WHERE aweme_id = %s AND %s::int IS NOT NULL),' \
  && expect_red "$T_COST" "test_the_runs_row_carries_the_sum_and_writing_it_again_changes_nothing"
restore

mp_mutation "the OpenRouter client has no response hook"
apply_mutation "$LLM" \
  '        event_hooks={"response": [make_response_hook(recorder)]},' \
  '        event_hooks={},' \
  && expect_red "$T_COST" \
    "test_the_cost_reaches_the_span_in_the_form_langfuse_ingests" \
    "test_a_pydantic_ai_agent_request_is_priced_by_the_same_hook"
restore

mp_mutation "OCR does not use the priced client"
apply_mutation "$REGISTRY" \
  '        ocr=VisionOcr.from_settings(settings, llm_http or http),' \
  '        ocr=VisionOcr.from_settings(settings, http),' \
  && expect_red "$T_COST" "test_the_agents_models_and_ocr_share_the_priced_client"
restore

mp_mutation "the agents' model does not use the priced client"
apply_mutation "$LLM" '        http_client=http,' '        http_client=None,' \
  && expect_red "$T_COST" "test_the_agents_models_and_ocr_share_the_priced_client"
restore

mp_mutation "publish never sends the run's scores"
apply_mutation "$PUBLISH" '        await self._score(request)' '        pass' \
  && expect_red "$T_SCORES" "test_a_published_run_puts_every_score_on_its_own_trace"
restore

mp_mutation "a score's id is new each time, so a retried publish adds a second set"
apply_mutation "$SCORES" '        return f"{trace_id}-{name}"' '        return f"{trace_id}-{name}-{id(object())}"' \
  && expect_red "$T_SCORES" "test_sending_again_overwrites_the_same_scores"
restore

mp_mutation "a score names the session too, which Langfuse refuses"
apply_mutation "$SCORES" '                    "traceId": trace_id,' $'                    "traceId": trace_id,\n                    "sessionId": "s",' \
  && expect_red "$T_SCORES" "test_a_published_run_puts_every_score_on_its_own_trace"
restore

mp_mutation "a missing dub document fails the publish"
apply_mutation "$PUBLISH" '            except Exception as exc:  # noqa: BLE001 - a missing document costs its scores only' '            except ZeroDivisionError as exc:' \
  && expect_red "$T_SCORES" "test_a_missing_dub_document_costs_its_scores_not_the_run"
restore

mp_mutation "a refused score is not caught"
apply_mutation "$SCORES" '                except httpx.HTTPError as exc:' '                except ZeroDivisionError as exc:' \
  && expect_red "$T_SCORES" "test_langfuse_down_does_not_fail_the_run"
restore

mp_mutation "the client has no tracing interceptor"
apply_mutation "$CLIENT" '        interceptors=[tracing_interceptor()],' '        interceptors=[],' \
  && expect_red "$T_TRACE" "test_one_run_is_one_trace_with_every_step"
restore

mp_mutation "the provider span carries the request path"
apply_mutation "$HTTP_BASE" '            self._name, provider=self._provider, model=self._model, input_units=units' \
  '            self._name, provider=f"{self._provider} {path}", model=self._model, input_units=units' \
  && expect_red "$T_SPANS" "test_stt_counts_seconds_of_audio"
restore

mp_mutation "the provider span does not count the input"
apply_mutation "$SPANS" '        attributes[UNITS] = input_units' '        pass' \
  && expect_red "$T_SPANS" "test_tts_counts_characters_of_text"
restore

mp_mutation "the workflow does not tell publish that a line was flagged"
apply_mutation "$WORKFLOW" '                    safety_flag=bool(flags),' '                    safety_flag=False,' \
  && expect_red "$T_LOOP" "test_publish_is_given_the_track_it_scores_and_whether_a_line_was_flagged"
restore

mp_mutation "the frames' own OCR answers are not stored"
apply_mutation "$DETECT" '            frame_boxes=tuple(boxes),' '            frame_boxes=(),' \
  && expect_red "$T_PREP" "test_every_frames_own_answer_is_kept_so_the_region_can_be_rebuilt"
restore

mp_mutation "a line that drops a glossary term its source says is not redone"
apply_mutation "$SINGLE" '            if check_terms and (lost := missing_terms(context, seg.text, text)):' \
  '            if False and (lost := missing_terms(context, seg.text, text)):' \
  && expect_red "$T_ONE" "test_a_line_that_drops_a_glossary_term_its_source_says_is_redone_with_the_term"
restore

mp_finish

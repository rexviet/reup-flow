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
CALLS=src/localization/observability/llm_calls.py
LLM=src/localization/providers/llm.py
REGISTRY=src/localization/providers/registry.py
PUBLISH=src/localization/activities/publish.py

T_COST=tests/observability/test_llm_cost.py

mp_init "$USAGE" "$CALLS" "$LLM" "$REGISTRY" "$PUBLISH"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)

mp_baseline "$T_COST"

mp_mutation "the span is not marked as a generation, so Langfuse drops its cost"
apply_mutation "$USAGE" '    attributes = {OBSERVATION_TYPE: "generation"}' '    attributes = {}' \
  && expect_red "$T_COST" "test_the_cost_reaches_the_span_in_the_form_langfuse_ingests"
restore

mp_mutation "the span carries a cost of zero instead of the reported one"
apply_mutation "$USAGE" '"total": usage.cost_usd})' '"total": 0.0})' \
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

mp_finish

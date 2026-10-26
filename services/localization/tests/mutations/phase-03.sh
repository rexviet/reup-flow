#!/usr/bin/env bash
#
# Mutation suite for the localization foundation guards:
#
#   - workflow code may import only its allowlist     (tests/architecture)
#   - no payload over 256 KiB crosses Temporal        (tests/test_size_guard_codec.py)
#   - no credential reaches input, history, spans or  (tests/test_secret_canary.py)
#     output
#   - the stored EditSpec schema matches the model    (tests/test_edit_spec_schema_drift.py)
#
# A guard is evidence only if breaking its invariant turns it red, and the
# controls say WHICH mechanism carries it. Protocol and rules: see
# scripts/lib/mutation-protocol.sh.
#
# Run from anywhere:  bash services/localization/tests/mutations/phase-03.sh
# Needs `uv sync` in services/localization. No database or MinIO.

set -u

cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=../../../../scripts/lib/mutation-protocol.sh
source ../../scripts/lib/mutation-protocol.sh

WORKFLOWS=src/localization/workflows/__init__.py
CLIENT=src/localization/temporal/client.py
VOICE=src/localization/contracts/voice.py
CONFIG=src/localization/config.py
MODELS=src/localization/contracts/models.py

ARCH=tests/architecture/test_workflow_imports.py
CODEC=tests/test_size_guard_codec.py
CANARY=tests/test_secret_canary.py
DRIFT=tests/test_edit_spec_schema_drift.py

mp_init "$WORKFLOWS" "$CLIENT" "$VOICE" "$CONFIG" "$MODELS"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)

mp_baseline "$ARCH $CODEC $CANARY $DRIFT"

# --- 1. workflow code imports `os` -------------------------------------------
mp_mutation "a workflow module imports os"
apply_mutation "$WORKFLOWS" '@append' $'\nimport os\n' \
  && expect_red "$ARCH" "test_real_workflows_import_only_the_allowlist"
restore

# --- 2. the codec is not attached ---------------------------------------------
mp_mutation "the data converter has no SizeGuardCodec"
apply_mutation "$CLIENT" \
  'return DataConverter(payload_codec=SizeGuardCodec())' \
  'return DataConverter()' \
  && expect_red "$CODEC" \
    "test_activity_result_over_the_limit_fails_the_workflow" \
    "test_workflow_input_over_the_limit_is_refused_by_the_client"
restore

# --- 3. the guard rides on a PayloadConverter instead of a codec ---------------
# PydanticAIPlugin installs its own payload converter and keeps only the codec,
# so a size check hung on a converter is silently discarded.
mp_mutation "the size guard is a custom PayloadConverter, not a PayloadCodec"
apply_mutation "$CLIENT" \
  'return DataConverter(payload_codec=SizeGuardCodec())' \
  $'from temporalio.converter import DefaultPayloadConverter\n    from temporalio.exceptions import ApplicationError\n\n    class GuardConverter(DefaultPayloadConverter):\n        def to_payloads(self, values):\n            payloads = super().to_payloads(values)\n            for p in payloads:\n                if len(p.data) > 256 * 1024:\n                    raise ApplicationError("too large", type="PayloadTooLarge", non_retryable=True)\n            return payloads\n\n    return DataConverter(payload_converter_class=GuardConverter)' \
  && expect_red "$CODEC" \
    "test_activity_result_over_the_limit_fails_the_workflow" \
    "test_workflow_input_over_the_limit_is_refused_by_the_client"
restore

# --- 4. a credential leaks into the workflow input -----------------------------
mp_mutation "VoiceRef carries the VoiceStudio API key into the workflow input"
apply_mutation "$VOICE" \
  '    settings: VoiceStudioSettings' \
  $'    settings: VoiceStudioSettings\n    upstream: str = ""' \
  "$CONFIG" \
  'provider="voicestudio", voice_id=settings.voice_id, settings=VoiceStudioSettings()' \
  $'provider="voicestudio",\n        voice_id=settings.voice_id,\n        settings=VoiceStudioSettings(),\n        upstream=settings.voicestudio_api_key.get_secret_value(),' \
  && expect_red "$CANARY" "test_no_canary_reaches_history_spans_or_output"
restore

# --- 5. the model changes, the stored schema does not -------------------------
mp_mutation "EditSpec gains a field and contracts/edit-spec.schema.json is not regenerated"
apply_mutation "$MODELS" \
  '    fps: PositiveInt' \
  $'    fps: PositiveInt\n    fps_hint: PositiveInt = 30' \
  && expect_red "$DRIFT" "test_stored_schema_matches_the_model"
restore

# --- control A: an allowed import must not trip the architecture guard ---------
# Shows the guard is an allowlist, not "any import in workflows/ fails".
mp_mutation "CONTROL: a workflow module imports only allowlisted names"
apply_mutation "$WORKFLOWS" '@append' \
  $'\nfrom datetime import timedelta\nfrom temporalio import workflow\n' \
  && expect_green "$ARCH" "allowlisted-imports"
restore

# --- control B: a non-secret field must not trip the canary --------------------
# Shows the canary keys on the secret itself, not on any change to VoiceRef.
mp_mutation "CONTROL: VoiceRef gains a field filled from a non-secret setting"
apply_mutation "$VOICE" \
  '    settings: VoiceStudioSettings' \
  $'    settings: VoiceStudioSettings\n    upstream: str = ""' \
  "$CONFIG" \
  'provider="voicestudio", voice_id=settings.voice_id, settings=VoiceStudioSettings()' \
  $'provider="voicestudio",\n        voice_id=settings.voice_id,\n        settings=VoiceStudioSettings(),\n        upstream=settings.voice_id,' \
  && expect_green "$CANARY" "non-secret-field"
restore

mp_finish

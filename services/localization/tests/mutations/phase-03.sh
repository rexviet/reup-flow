#!/usr/bin/env bash
#
# Mutation suite for the localization foundation guards:
#
#   - workflow code may import only its allowlist     (tests/architecture)
#   - no payload over 256 KiB crosses Temporal        (tests/test_size_guard_codec.py)
#   - no credential reaches input, history, spans or  (tests/test_secret_canary.py)
#     output
#   - the stored EditSpec schema matches the model    (tests/test_edit_spec_schema_drift.py)
#   - the import scanner itself cannot be walked      (tests/architecture)
#     around (parent __init__ files, __import__/exec)
#   - an oversize payload built in a workflow fails   (tests/test_payload_in_workflow.py)
#     it cleanly instead of retrying forever
#   - only a real 2xx WAV is ever treated as audio    (tests/providers)
#   - a source video key must live under source/      (tests/test_contract_hardening.py)
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
SCANNER=tests/architecture/import_scanner.py
LIMITS=src/localization/contracts/limits.py
HTTP_BASE=src/localization/providers/base.py
TTS=src/localization/providers/openai_tts.py
IDS=src/localization/contracts/ids.py

ARCH=tests/architecture/test_workflow_imports.py
CODEC=tests/test_size_guard_codec.py
CANARY=tests/test_secret_canary.py
DRIFT=tests/test_edit_spec_schema_drift.py
PAYLOAD=tests/test_payload_in_workflow.py
PROVIDERS=tests/providers/test_openai_compat.py
HARDENING=tests/test_contract_hardening.py

mp_init "$WORKFLOWS" "$CLIENT" "$VOICE" "$CONFIG" "$MODELS" "$SCANNER" "$LIMITS" "$HTTP_BASE" "$TTS" "$IDS"
MP_TEST_CMD=(uv run pytest -q -p no:cacheprovider)

mp_baseline "$ARCH $CODEC $CANARY $DRIFT $PAYLOAD $PROVIDERS $HARDENING"

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
  '    settings: VoiceStudioSettings | OpenAICompatSettings' \
  $'    settings: VoiceStudioSettings | OpenAICompatSettings\n    upstream: str = ""' \
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

# --- 6. the import scanner skips the __init__ of a parent package -------------
mp_mutation "the import scanner no longer scans parent packages' __init__.py"
apply_mutation "$SCANNER" \
  '    for i in range(1, end):' \
  '    for i in range(1, 1):' \
  && expect_red "$ARCH" \
    "test_a_parent_packages_init_is_executed_so_it_is_scanned" \
    "test_a_deeply_nested_import_scans_every_parent_init"
restore

# --- 7. the import scanner stops looking for __import__/exec/open --------------
mp_mutation "the import scanner no longer detects dynamic escapes"
apply_mutation "$SCANNER" \
  '        for escape in _dynamic_escapes(path):' \
  '        for escape in []:' \
  && expect_red "$ARCH" "test_dynamic_escapes_from_the_allowlist_are_rejected"
restore

# --- 8. the payload pre-check does nothing --------------------------------------
mp_mutation "ensure_payload_fits never raises"
apply_mutation "$LIMITS" '    if size > limit:' '    if False:' \
  && expect_red "$PAYLOAD" "test_precheck_fails_the_workflow_fast_and_non_retryably"
restore

# --- 9. a redirect counts as success ---------------------------------------------
mp_mutation "the provider client accepts 3xx as success"
apply_mutation "$HTTP_BASE" \
  'if not 200 <= response.status_code < 300:' \
  'if response.status_code >= 400:' \
  && expect_red "$PROVIDERS" "test_a_redirect_is_not_a_success_for_stt"
restore

# --- 10. any non-empty body is accepted as TTS audio ------------------------------
mp_mutation "TTS accepts any non-empty body as a WAV"
apply_mutation "$TTS" \
  'if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":' \
  'if not audio:' \
  && expect_red "$PROVIDERS" "test_a_200_that_is_not_a_wav_is_never_returned_as_audio"
restore

# --- 11. the source video can live anywhere -----------------------------------------
mp_mutation "LocalizationInput.video_key is no longer required to be under source/"
apply_mutation "$IDS" '    if not value.startswith(SOURCE_PREFIX):' '    if False:' \
  && expect_red "$HARDENING" "test_the_source_video_must_live_under_source"
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
  '    settings: VoiceStudioSettings | OpenAICompatSettings' \
  $'    settings: VoiceStudioSettings | OpenAICompatSettings\n    upstream: str = ""' \
  "$CONFIG" \
  'provider="voicestudio", voice_id=settings.voice_id, settings=VoiceStudioSettings()' \
  $'provider="voicestudio",\n        voice_id=settings.voice_id,\n        settings=VoiceStudioSettings(),\n        upstream=settings.voice_id,' \
  && expect_green "$CANARY" "non-secret-field"
restore

mp_finish

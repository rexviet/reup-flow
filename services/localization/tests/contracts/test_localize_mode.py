import pytest
from pydantic import ValidationError

from localization.contracts import LocalizationInput, ReviewProjectManifest

REVIEW_VOICE = {"provider": "voicestudio", "voice_id": "v1", "settings": {"speed": 1.0}}
VBEE_VOICE = {"provider": "vbee", "voice_id": "vbee-configured-voice", "settings": {}}


def _input(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "aweme_id": "7312345678901234567",
        "channel_id": "chan_1-x",
        "generation": 0,
        "video_key": "source/7312345678901234567.mp4",
        "target_lang": "vi",
        "voice": REVIEW_VOICE,
        "delivery_mode": "review_project",
    }
    return {**base, **overrides}


def test_an_input_without_a_mode_localizes_in_full():
    assert LocalizationInput.model_validate(_input()).localize_mode == "full"


def test_a_subtitles_only_input_is_accepted_for_a_review_project():
    spec = LocalizationInput.model_validate(_input(localize_mode="subtitles_only"))
    assert spec.localize_mode == "subtitles_only"


def test_a_subtitles_only_input_needs_a_review_project():
    with pytest.raises(ValidationError, match="subtitles_only"):
        LocalizationInput.model_validate(
            _input(localize_mode="subtitles_only", delivery_mode="legacy_render")
        )


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValidationError):
        LocalizationInput.model_validate(_input(localize_mode="captions"))


def test_a_manifest_written_before_the_mode_reads_as_full():
    assert ReviewProjectManifest.model_fields["localize_mode"].default == "full"


# --- a dubbed run writes exactly what it wrote before the mode existed --------------------
# Every contract forbids unknown fields, so a worker or render machine still on the code
# before the mode would refuse an input or a manifest that carries it. A dubbed run leaves
# it out; only a subtitles-only run, which such a machine could not handle anyway, has it.


def _payload(spec: LocalizationInput) -> dict:
    import json

    from temporalio.contrib.pydantic import pydantic_data_converter

    (payload,) = pydantic_data_converter.payload_converter.to_payloads([spec])
    return json.loads(payload.data)


def test_a_dubbed_input_reaches_temporal_without_the_mode():
    assert "localize_mode" not in _payload(LocalizationInput.model_validate(_input()))


def test_a_subtitles_only_input_reaches_temporal_with_the_mode_and_reads_back():
    spec = LocalizationInput.model_validate(_input(localize_mode="subtitles_only"))
    body = _payload(spec)
    assert body["localize_mode"] == "subtitles_only"
    assert LocalizationInput.model_validate(body) == spec


def test_a_dubbed_input_reads_back_as_dubbed():
    spec = LocalizationInput.model_validate(_input())
    assert LocalizationInput.model_validate_json(spec.model_dump_json()) == spec


def test_a_dubbed_manifest_is_written_without_the_mode(review_store, tmp_path):
    import json

    from tests.support.review_project import project_inputs

    from localization.activities.review_project import resolve_project

    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    _, manifest, _ = resolve_project(
        review_store, builder._build(spec, dub, ingest, subs, None).project_key
    )
    assert "localize_mode" not in json.loads(manifest.model_dump_json())
    assert "localize_mode" not in manifest.model_dump()


def test_a_subtitles_only_manifest_is_written_with_the_mode(review_store, tmp_path):
    import json

    from tests.support.review_project import project_inputs

    from localization.activities.review_project import resolve_project

    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path, subtitles_only=True)
    ref = builder._build(spec, dub, ingest, subs, None)
    raw = review_store.get_bytes(
        json.loads(review_store.get_bytes(ref.project_key, produced_by="t"))["manifest_key"],
        produced_by="t",
    )
    assert json.loads(raw)["localize_mode"] == "subtitles_only"
    assert resolve_project(review_store, ref.project_key)[1].localize_mode == "subtitles_only"

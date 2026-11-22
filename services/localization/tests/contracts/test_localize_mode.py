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

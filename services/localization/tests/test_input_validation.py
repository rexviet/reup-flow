import pytest
from pydantic import ValidationError

from localization.config import ConfigError, Settings, check_voicestudio_url
from localization.contracts import (
    DubSegment,
    LocalizationInput,
    ReviewVerdict,
    VoiceRef,
    VoiceStudioSettings,
)
from localization.contracts.voice import ProviderSettings
from localization.storage.object_store import (
    output_key,
    render_key,
    review_key,
    tts_cache_key,
    work_key,
)

SHA = "a" * 64
VOICE = {"provider": "voicestudio", "voice_id": "v1", "settings": {"speed": 1.0}}


def _input(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "aweme_id": "7312345678901234567",
        "channel_id": "chan_1-x",
        "generation": 0,
        "video_key": "source/7312345678901234567.mp4",
        "target_lang": "vi",
        "voice": VOICE,
    }
    return {**base, **overrides}


BAD_AWEME_IDS = ["", "../etc", "12/34", "١٢٣", "123\n", " 123", "1" * 33, "12a", "-1"]


@pytest.mark.parametrize("bad", BAD_AWEME_IDS)
def test_aweme_id_is_rejected_by_the_contract_and_every_key_function(bad: str) -> None:
    with pytest.raises(ValidationError):
        LocalizationInput.model_validate(_input(aweme_id=bad))
    for build in (
        lambda: work_key(bad, 0, "t.json"),
        lambda: render_key(bad, SHA),
        lambda: review_key(bad, 0),
        lambda: output_key(bad),
    ):
        with pytest.raises(ValueError):
            build()


@pytest.mark.parametrize("bad", ["", "a/b", "a b", "a" * 129, "é", "x\n"])
def test_channel_id_rejects(bad: str) -> None:
    with pytest.raises(ValidationError):
        LocalizationInput.model_validate(_input(channel_id=bad))


def test_valid_input_round_trips() -> None:
    parsed = LocalizationInput.model_validate(_input())
    assert parsed.voice.settings.speed == 1.0
    assert LocalizationInput.model_validate_json(parsed.model_dump_json()) == parsed


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "..", ".hidden", "a..b", "a b"])
def test_work_key_name_cannot_add_a_path_segment(bad: str) -> None:
    with pytest.raises(ValueError):
        work_key("123", 1, bad)


def test_keys_have_the_documented_shape() -> None:
    assert work_key("123", 2, "script.json") == "work/123/r2/script.json"
    assert tts_cache_key(SHA) == f"cache/tts/{SHA}.wav"
    assert render_key("123", SHA) == f"work/123/render-{SHA}.mp4"
    assert review_key("123", 3) == "review/123-g3.mp4"
    assert output_key("123") == "output/123.mp4"


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "../" + "a" * 61, ""])
def test_hashes_must_be_lowercase_hex_sha256(bad: str) -> None:
    with pytest.raises(ValueError):
        tts_cache_key(bad)


def test_negative_round_is_rejected() -> None:
    with pytest.raises(ValueError):
        work_key("123", -1, "x.json")


def test_contract_models_are_frozen_and_closed() -> None:
    parsed = LocalizationInput.model_validate(_input())
    with pytest.raises(ValidationError):
        parsed.aweme_id = "9"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        LocalizationInput.model_validate(_input(surprise=1))


@pytest.mark.parametrize("ratio", [0.84, 1.16, 0.0, 2.0])
def test_dub_segment_stretch_ratio_is_bounded(ratio: float) -> None:
    with pytest.raises(ValidationError):
        DubSegment(
            id=0,
            text_vi="x",
            tts_key="k",
            duration_ms=10,
            stretch_ratio=ratio,
            overflow_unresolved=False,
        )


@pytest.mark.parametrize("ratio", [0.85, 1.0, 1.15])
def test_dub_segment_accepts_the_bounds(ratio: float) -> None:
    assert (
        DubSegment(
            id=0,
            text_vi="x",
            tts_key="k",
            duration_ms=10,
            stretch_ratio=ratio,
            overflow_unresolved=False,
        ).stretch_ratio
        == ratio
    )


def test_review_verdict_uses_pass_on_the_wire() -> None:
    verdict = ReviewVerdict.model_validate(
        {"pass": True, "scores": {"fidelity": 1, "timing": 1, "visual": 1}, "issues": []}
    )
    assert verdict.passed is True
    assert '"pass":true' in verdict.model_dump_json(by_alias=True)


def test_voice_settings_are_a_closed_model() -> None:
    with pytest.raises(ValidationError):
        VoiceRef.model_validate({**VOICE, "settings": {"speed": 1.0, "api_key": "x"}})
    with pytest.raises(ValidationError):
        VoiceRef.model_validate({**VOICE, "api_key": "x"})


@pytest.mark.parametrize("name", ["api_key", "token", "client_secret", "Password", "KEYS"])
def test_provider_settings_reject_credential_looking_fields(name: str) -> None:
    with pytest.raises(TypeError, match="credentials"):
        type("Bad", (ProviderSettings,), {"__annotations__": {name: str}})


def test_voicestudio_settings_has_no_credential_looking_field() -> None:
    assert not [n for n in VoiceStudioSettings.model_fields if "key" in n or "token" in n]


# --- VOICESTUDIO_URL --------------------------------------------------------


def test_https_url_is_always_accepted() -> None:
    check_voicestudio_url("https://voice.example.com", "")


@pytest.mark.parametrize(
    "url, cidrs",
    [
        ("http://voice.example.com", "10.0.0.0/8"),  # hostname over http
        ("http://8.8.8.8", "10.0.0.0/8"),  # public IP over http
        ("http://10.1.2.3", ""),  # no allow-list configured
        ("ftp://10.1.2.3", "10.0.0.0/8"),
        ("https://user:pw@voice.example.com", ""),  # embedded credentials
        ("http://[::1]", "10.0.0.0/8"),
        ("voice.example.com", ""),
    ],
)
def test_unsafe_voicestudio_urls_refuse_to_start(url: str, cidrs: str) -> None:
    with pytest.raises(ConfigError):
        check_voicestudio_url(url, cidrs)


def test_http_inside_the_allow_listed_cidr_is_accepted() -> None:
    check_voicestudio_url("http://192.168.1.50:8000", "10.0.0.0/8, 192.168.0.0/16")


def test_settings_refuse_to_load_with_an_unsafe_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICESTUDIO_URL", "http://203.0.113.9")
    monkeypatch.delenv("VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS", raising=False)
    with pytest.raises(ValidationError, match="plain http") as info:
        Settings()
    assert "203.0.113.9" not in str(info.value)


def test_settings_accept_an_allow_listed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICESTUDIO_URL", "http://10.0.0.7:9000")
    monkeypatch.setenv("VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS", "10.0.0.0/8")
    assert Settings().voicestudio_url == "http://10.0.0.7:9000"


def test_settings_do_not_print_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICESTUDIO_API_KEY", "canary-visible-check")
    monkeypatch.setenv("OPENROUTER_API_KEY", "canary-visible-check-2")
    text = repr(Settings()) + str(Settings())
    assert "canary-visible-check" not in text

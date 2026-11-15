"""Provider-neutral contracts, storage keys, cross-field checks, endpoint URLs and
the environment contract."""

import re
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from pydantic import ValidationError

from localization.config import ConfigError, Settings
from localization.contracts import (
    Caption,
    LocalizationInput,
    LocalizedSegment,
    SttSegment,
    SttWord,
    VoiceRef,
    VoiceStudioSettings,
)
from localization.contracts.voice import OpenAICompatSettings
from localization.providers.base import OcrProvider, StemSeparator, SttProvider, TtsProvider
from localization.providers.demucs import DemucsSeparator
from localization.providers.openai_tts import OpenAICompatTts
from localization.providers.registry import Providers, build_providers
from localization.storage.object_store import ObjectStore

REPO_ROOT = Path(__file__).resolve().parents[3]


# --- VoiceRef is discriminated by provider ----------------------------------


def test_settings_are_parsed_as_the_class_of_their_provider() -> None:
    studio = VoiceRef.model_validate(
        {"provider": "voicestudio", "voice_id": "v", "settings": {"instructions": "calm"}}
    )
    compat = VoiceRef.model_validate(
        {"provider": "openai_compat", "voice_id": "v", "settings": {"speed": 1.5}}
    )
    assert type(studio.settings) is VoiceStudioSettings
    assert type(compat.settings) is OpenAICompatSettings
    # And the same through JSON, which is how Temporal carries it.
    assert VoiceRef.model_validate_json(compat.model_dump_json()) == compat
    assert VoiceRef.model_validate_json(studio.model_dump_json()) == studio


def test_settings_that_belong_to_another_provider_are_rejected() -> None:
    with pytest.raises(ValidationError):  # `instructions` is VoiceStudio-only
        VoiceRef.model_validate(
            {"provider": "openai_compat", "voice_id": "v", "settings": {"instructions": "x"}}
        )
    with pytest.raises(ValidationError):  # right fields, wrong class
        VoiceRef(provider="openai_compat", voice_id="v", settings=VoiceStudioSettings())
    with pytest.raises(ValidationError):
        VoiceRef.model_validate({"provider": "elevenlabs", "voice_id": "v", "settings": {}})


# --- storage keys ------------------------------------------------------------

BAD_KEYS = [
    "",
    "/abs/key",
    "../x",
    "a/../b",
    "a//b",
    "a/./b",
    "a/b/",
    "a\\b",
    "a b",
    "é",
    "a\nb",
    "x" * 1025,
    "..",
    ".hidden/key",
]


@pytest.mark.parametrize("key", BAD_KEYS, ids=range(len(BAD_KEYS)))
def test_bad_storage_keys_are_refused_in_contracts(key: str) -> None:
    from localization.contracts import SubtitleTrack, Transcript

    with pytest.raises(ValidationError):
        Transcript.model_validate({"transcript_key": key, "segment_count": 0})
    with pytest.raises(ValidationError):
        SubtitleTrack.model_validate({"subtitle_key": key, "region": None})


@pytest.mark.parametrize("key", BAD_KEYS, ids=range(len(BAD_KEYS)))
def test_bad_storage_keys_are_refused_by_every_object_store_call(key: str, tmp_path: Path) -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        store = ObjectStore(client, "test-bucket")
        calls = [
            lambda: store.put_bytes(key, b"x"),
            lambda: store.put_file(key, tmp_path / "f"),
            lambda: store.get_bytes(key, produced_by="s"),
            lambda: store.download_file(key, tmp_path / "f", produced_by="s"),
            lambda: store.exists(key),
        ]
        for call in calls:
            with pytest.raises(ValueError):
                call()


@pytest.mark.parametrize(
    "key", ["source/7312.mp4", "work/1/r0/a.json", "cache/tts/" + "a" * 64 + ".wav", "a=b/c-d_e"]
)
def test_ordinary_keys_pass(key: str) -> None:
    from localization.contracts.ids import validate_storage_key

    assert validate_storage_key(key) == key


@pytest.mark.parametrize("key", ["work/1/video.mp4", "output/1.mp4", "video.mp4", "sourcery/1.mp4"])
def test_the_source_video_must_live_under_source(key: str) -> None:
    voice = {"provider": "voicestudio", "voice_id": "v", "settings": {}}
    base = {
        "aweme_id": "1",
        "channel_id": "c",
        "generation": 0,
        "target_lang": "vi",
        "voice": voice,
    }
    with pytest.raises(ValidationError):
        LocalizationInput.model_validate({**base, "video_key": key})
    assert LocalizationInput.model_validate({**base, "video_key": "source/1.mp4"})


# --- cross-field checks -----------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: SttWord(text="a", start_ms=10, end_ms=9),
        lambda: SttSegment(id=0, start_ms=10, end_ms=9, text="a"),
        lambda: LocalizedSegment(
            id=0, start_ms=10, end_ms=9, slot_ms=0, src_zh="", text_vi="", on_screen=False
        ),
        lambda: Caption(segment_id=0, start_ms=10, end_ms=9, text="a", font_px=48),
    ],
)
def test_a_span_cannot_end_before_it_starts(build) -> None:
    with pytest.raises(ValidationError, match="end_ms"):
        build()


def test_a_zero_length_span_is_allowed() -> None:
    assert SttWord(text="a", start_ms=5, end_ms=5)


# --- endpoint URLs -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://openrouter.ai/api/v1", "http://203.0.113.5/v1", "ftp://x/v1", "https://u:p@x/v1"],
)
def test_openrouter_base_url_must_be_https_or_an_allow_listed_address(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", url)
    with pytest.raises(ValidationError) as info:
        Settings()
    assert url not in str(info.value)


def test_openrouter_base_url_accepts_https_and_the_default_private_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    Settings()
    monkeypatch.setenv("OPENROUTER_BASE_URL", "http://100.64.1.2:4000/v1")
    Settings()


def test_the_default_private_http_range_is_the_tailscale_one() -> None:
    assert Settings().voicestudio_allow_private_http_cidrs == "100.64.0.0/10"
    with pytest.raises(ConfigError):
        from localization.config import check_endpoint_url

        check_endpoint_url(
            "X", "http://192.168.1.5", Settings().voicestudio_allow_private_http_cidrs
        )


# --- provider interfaces ------------------------------------------------------


class _FakeStt:
    async def transcribe(self, audio, *, filename="a.wav", language=None):
        return None


class _FakeTts:
    async def synthesize(self, text, voice):
        return b"RIFFxxxxWAVE"


class _FakeOcr:
    async def detect_region(self, frame_png, width, height):
        return None


class _FakeSeparator:
    def separate(self, input_path):
        return (input_path, input_path, input_path)


def test_the_registry_accepts_any_implementation_of_the_interfaces() -> None:
    providers = Providers(stt=_FakeStt(), tts=_FakeTts(), ocr=_FakeOcr(), demucs=_FakeSeparator())
    assert isinstance(providers.stt, SttProvider)
    assert isinstance(providers.tts, TtsProvider)
    assert isinstance(providers.ocr, OcrProvider)
    assert isinstance(providers.demucs, StemSeparator)


def test_the_real_providers_satisfy_the_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICESTUDIO_URL", "https://voice.example.com")
    monkeypatch.setenv("STT_MODEL", "s")
    monkeypatch.setenv("TTS_MODEL", "t")
    monkeypatch.setenv("OCR_MODEL", "o")
    monkeypatch.setenv("OCR_BBOX_FORMAT", "xyxy_px")
    providers = build_providers(Settings())
    assert isinstance(providers.stt, SttProvider)
    assert isinstance(providers.tts, TtsProvider)
    assert isinstance(providers.ocr, OcrProvider)
    assert isinstance(providers.demucs, StemSeparator)
    assert isinstance(OpenAICompatTts("https://x", None, "m"), TtsProvider)


# --- demucs child environment -------------------------------------------------


def test_demucs_child_gets_a_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import shutil
    import stat

    monkeypatch.setenv("OPENROUTER_API_KEY", "canary-should-not-leak")
    monkeypatch.setenv("LOC_S3_SECRET_ACCESS_KEY", "canary-should-not-leak")
    binary = tmp_path / "fake-demucs"
    binary.write_text(
        '#!/bin/sh\nout="$3"\nmkdir -p "$out/m"\necho v > "$out/m/vocals.wav"\n'
        'echo b > "$out/m/no_vocals.wav"\nenv > "$out/env.txt"\n'
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    work_dir, *_ = DemucsSeparator(str(binary)).separate(audio)
    try:
        child_env = (work_dir / "env.txt").read_text()
        assert "canary-should-not-leak" not in child_env
        assert "OPENROUTER_API_KEY" not in child_env
        assert f"TMPDIR={work_dir}" in child_env
        assert f"PATH={os.environ['PATH']}" in child_env
    finally:
        shutil.rmtree(work_dir)


# --- the environment contract --------------------------------------------------


def test_every_setting_is_documented_in_env_example() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, flags=re.M))
    missing = [n.upper() for n in Settings.model_fields if n.upper() not in documented]
    assert missing == []


def test_env_example_carries_no_real_values_for_secrets() -> None:
    text = (REPO_ROOT / ".env.example").read_text()
    for name in ("OPENROUTER_API_KEY", "VOICESTUDIO_API_KEY", "LOC_DATABASE_URL"):
        assert re.search(rf"^{name}=$", text, flags=re.M), name


def test_a_sung_segment_has_no_clip_and_a_spoken_one_needs_one() -> None:
    from localization.contracts import DubSegment

    base = {"id": 0, "text_vi": "lời", "duration_ms": 0, "stretch_ratio": 1.0}
    DubSegment(**base, overflow_unresolved=False, keep_original=True)
    with pytest.raises(ValidationError):
        DubSegment(**base, overflow_unresolved=False)
    with pytest.raises(ValidationError):
        DubSegment(
            **base, overflow_unresolved=False, keep_original=True, tts_key="cache/tts/a.wav"
        )

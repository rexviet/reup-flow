"""The voice check: the source downloaded, probed and decoded for real, and a threshold on the
share of its audio the detector calls speech.

The decisions run on a stand-in detector. The real Silero model is run too, on what can be
made honestly here: silence and a pure tone, which must measure no speech at all. There is no
recording of real speech in the repository, and a tone is no stand-in for one, so the speech
side of the real model is checked on the channel's own videos by hand, not here."""

import math
import struct
from pathlib import Path

import pytest
from pydantic import ValidationError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.check_voice import (
    SAMPLE_RATE,
    CheckVoiceActivity,
    SileroSpeechDetector,
    speech_share,
)
from localization.config import Settings
from localization.contracts.models import LocalizationResult, VoiceCheck
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.storage.object_store import ObjectStore
from tests.support.file_s3 import FileS3
from tests.support.inputs import make_input
from tests.support.voice_fakes import FakeDetector, SourceKind, source_video

SPEC = make_input("7001", 3)


@pytest.fixture
def store(tmp_path: Path) -> ObjectStore:
    return ObjectStore(FileS3(tmp_path), "reup-media")


def put(store: ObjectStore, kind: SourceKind = "picture_and_sound") -> None:
    store.put_bytes(SPEC.video_key, source_video(kind), content_type="video/mp4")


async def check(
    store: ObjectStore,
    detector: object,
    *,
    min_speech_share: float = 0.05,
    enabled: bool = True,
) -> VoiceCheck:
    act = CheckVoiceActivity(
        store,
        detector,  # type: ignore[arg-type]
        min_speech_share=min_speech_share,
        enabled=enabled,
    )
    return await ActivityEnvironment().run(act.check_voice, SPEC)


# --- the decision -------------------------------------------------------------------------


async def test_a_video_under_the_threshold_has_no_voice(store: ObjectStore) -> None:
    put(store)
    detector = FakeDetector(share=0.037)
    result = await check(store, detector)
    assert result.no_voice and result.has_video
    assert result.speech_share == pytest.approx(0.037)
    # The source's own audio was decoded and measured: two seconds of it.
    (seconds,) = detector.measured
    assert 1.9 < seconds < 2.1


async def test_a_video_at_the_threshold_has_a_voice(store: ObjectStore) -> None:
    put(store)
    result = await check(store, FakeDetector(share=0.05), min_speech_share=0.05)
    assert not result.no_voice
    assert result.speech_share == pytest.approx(0.05)


async def test_the_threshold_is_the_one_the_worker_was_given(store: ObjectStore) -> None:
    put(store)
    assert (await check(store, FakeDetector(share=0.3), min_speech_share=0.5)).no_voice
    assert not (await check(store, FakeDetector(share=0.3), min_speech_share=0.25)).no_voice


async def test_a_video_without_audio_has_no_voice_and_is_not_decoded(store: ObjectStore) -> None:
    put(store, "picture_only")
    detector = FakeDetector(share=1.0)
    result = await check(store, detector)
    assert result == VoiceCheck(speech_share=0.0, has_video=True, no_voice=True)
    assert detector.measured == []


async def test_a_file_without_a_picture_says_so_and_is_not_heard(store: ObjectStore) -> None:
    """It is skipped whatever its audio holds, so its audio is not decoded or measured: the
    share is None, not a 0 that would claim it was heard."""
    put(store, "sound_only")
    detector = FakeDetector(share=0.9)
    result = await check(store, detector)
    assert result == VoiceCheck(speech_share=None, has_video=False, no_voice=False)
    assert detector.measured == []


async def test_switched_off_it_reads_nothing_and_finds_a_voice(store: ObjectStore) -> None:
    # No source in the bucket: a check that tried to read it would fail.
    detector = FakeDetector(share=0.0)
    result = await check(store, detector, enabled=False)
    assert result == VoiceCheck()
    assert not result.no_voice and result.speech_share is None
    assert detector.measured == []


def test_the_share_is_bounded_and_zero_without_audio() -> None:
    assert speech_share(FakeDetector(share=2.0), b"\0\0" * SAMPLE_RATE) == 1.0
    assert speech_share(FakeDetector(share=-1.0), b"\0\0" * SAMPLE_RATE) == 0.0
    assert speech_share(FakeDetector(share=1.0), b"") == 0.0


# --- failures -------------------------------------------------------------------------------


async def test_a_missing_source_fails_for_good_naming_the_downloader(store: ObjectStore) -> None:
    with pytest.raises(ApplicationError) as caught:
        await check(store, FakeDetector())
    assert caught.value.type == "ArtifactMissingError"
    assert caught.value.non_retryable
    assert caught.value.details[0] == "download"


async def test_a_file_that_is_not_a_video_fails_for_good(store: ObjectStore) -> None:
    store.put_bytes(SPEC.video_key, b"not a video at all", content_type="video/mp4")
    with pytest.raises(ApplicationError) as caught:
        await check(store, FakeDetector())
    assert caught.value.type == "PermanentProviderError"
    assert caught.value.non_retryable


async def test_a_detector_failure_keeps_its_kind(store: ObjectStore) -> None:
    put(store)
    with pytest.raises(ApplicationError) as permanent:
        await check(store, FakeDetector(fail=PermanentProviderError("vad: broken")))
    assert permanent.value.non_retryable
    with pytest.raises(ApplicationError) as transient:
        await check(store, FakeDetector(fail=TransientProviderError("vad: busy")))
    assert not transient.value.non_retryable


# --- the contract and the settings ------------------------------------------------------------


def test_a_result_recorded_before_the_check_still_parses() -> None:
    """A run recorded before a video could end unprepared always returned its preparation."""
    old = {
        "prep": {
            "ingest": {
                "vocals_key": "work/7001/r0/vocals.wav",
                "bgm_key": "work/7001/r0/bgm.wav",
                "duration_ms": 3000,
                "width": 1080,
                "height": 1920,
                "fps": 30,
            },
            "transcript": {"transcript_key": "work/7001/r0/transcript.json", "segment_count": 2},
            "subtitles": {"subtitle_key": "work/7001/r0/subtitles.json", "region": None},
        },
        "script": None,
    }
    assert LocalizationResult.model_validate(old).prep is not None
    published = LocalizationResult.model_validate(
        {
            "script": None,
            "publish": {"outcome": "published_original", "output_key": "output/7001.mp4"},
        }
    )
    assert published.prep is None and published.publish is not None


def test_the_share_must_be_a_share() -> None:
    for wrong in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            VoiceCheck(speech_share=wrong)


def test_the_settings_default_to_on_at_five_percent_and_refuse_a_share_outside_0_to_1() -> None:
    s = Settings()
    assert (s.voice_check_enabled, s.voice_check_min_speech_share) == (True, 0.05)
    assert s.voice_check_concurrency == 2
    for wrong in (-0.01, 1.01):
        with pytest.raises(ValidationError):
            Settings(voice_check_min_speech_share=wrong)


def test_the_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_CHECK_ENABLED", "false")
    monkeypatch.setenv("VOICE_CHECK_MIN_SPEECH_SHARE", "0.1")
    monkeypatch.setenv("VOICE_CHECK_CONCURRENCY", "3")
    s = Settings()
    assert (s.voice_check_enabled, s.voice_check_min_speech_share) == (False, 0.1)
    assert s.voice_check_concurrency == 3


# --- the real model ---------------------------------------------------------------------------


def _pcm(samples: list[float]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *(round(32767 * s) for s in samples))


def test_silero_hears_no_speech_in_silence() -> None:
    assert SileroSpeechDetector().speech_seconds(_pcm([0.0] * (SAMPLE_RATE * 3))) == 0.0


def test_silero_hears_no_speech_in_a_pure_tone() -> None:
    tone = [0.4 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE) for i in range(SAMPLE_RATE * 3)]
    assert SileroSpeechDetector().speech_seconds(_pcm(tone)) == 0.0


def test_silero_takes_an_odd_byte_and_no_audio() -> None:
    detector = SileroSpeechDetector()
    assert detector.speech_seconds(b"") == 0.0
    assert detector.speech_seconds(b"\0") == 0.0
    assert detector.speech_seconds(_pcm([0.0] * SAMPLE_RATE) + b"\0") == 0.0


async def test_the_real_check_finds_no_voice_in_a_video_of_a_tone(store: ObjectStore) -> None:
    """Download, probe, decode and the real model, end to end, on a video whose sound is a
    440 Hz tone: no speech, so the original would be published."""
    put(store)
    result = await check(store, SileroSpeechDetector())
    assert result == VoiceCheck(speech_share=0.0, has_video=True, no_voice=True)

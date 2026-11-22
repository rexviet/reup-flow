"""A subtitles-only run keeps the source's own sound: no separation, no voice made. The
source is decoded for real with ffmpeg; the store is a file-backed S3."""

import hashlib
import wave
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from localization.activities.layout_captions import check_one_translation
from localization.activities.source_audio import SourceAudioActivities
from localization.contracts import (
    DubSegmentsDocument,
    LocalizedScript,
    LocalizedScriptRef,
    LocalizedSegment,
    TranscriptDocument,
)
from localization.storage.object_store import ObjectStore, work_key_for
from tests.support.file_s3 import FileS3
from tests.support.inputs import make_input
from tests.support.voice_fakes import SourceKind, source_video

SPEC = make_input("7001", 3)


@pytest.fixture
def store(tmp_path: Path) -> ObjectStore:
    return ObjectStore(FileS3(tmp_path), "reup-media")


def put(store: ObjectStore, kind: SourceKind) -> None:
    store.put_bytes(SPEC.video_key, source_video(kind), content_type="video/mp4")


def wav_ms(store: ObjectStore, key: str, tmp_path: Path) -> int:
    path = tmp_path / "out.wav"
    store.download_file(key, path, produced_by="test")
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 2
        assert w.getframerate() == 44100
        return round(w.getnframes() / w.getframerate() * 1000)


async def test_the_source_sound_becomes_the_track_without_a_separation(store, tmp_path):
    put(store, "picture_and_sound")
    ingest = await ActivityEnvironment().run(SourceAudioActivities(store).source_audio, SPEC)
    assert ingest.bgm_key == work_key_for(SPEC, 0, "source-audio.wav")
    # No voice is separated: whatever reads the vocals reads the whole sound.
    assert ingest.vocals_key == ingest.bgm_key
    assert (ingest.width, ingest.height, ingest.fps) == (64, 64, 10)
    assert abs(ingest.duration_ms - 2000) <= 50
    assert abs(wav_ms(store, ingest.bgm_key, tmp_path) - 2000) <= 50


async def test_a_video_without_sound_gets_silence_of_its_length(store, tmp_path):
    put(store, "picture_only")
    ingest = await ActivityEnvironment().run(SourceAudioActivities(store).source_audio, SPEC)
    assert abs(wav_ms(store, ingest.bgm_key, tmp_path) - ingest.duration_ms) <= 50


async def test_the_empty_transcript_leaves_every_line_to_the_subtitles(store):
    made = await ActivityEnvironment().run(SourceAudioActivities(store).empty_transcript, SPEC)
    assert made.segment_count == 0
    doc = TranscriptDocument.model_validate_json(
        store.get_bytes(made.transcript_key, produced_by="test")
    )
    assert doc.segments == () and doc.words == ()


def _script(store: ObjectStore) -> LocalizedScriptRef:
    script = LocalizedScript(
        aweme_id=SPEC.aweme_id,
        version=2,
        prompt_version="test",
        segments=(
            LocalizedSegment(
                id=0,
                start_ms=0,
                end_ms=900,
                slot_ms=900,
                src_zh="你好",
                text_vi="Xin chào",
                on_screen=True,
            ),
            LocalizedSegment(
                id=1,
                start_ms=1000,
                end_ms=1800,
                slot_ms=800,
                src_zh="？",
                on_screen=True,
                text_vi="",
                status="unresolved",
                reason="unreadable",
            ),
        ),
    )
    key = work_key_for(SPEC, 0, "script.v2.json")
    store.put_bytes(key, script.model_dump_json().encode())
    return LocalizedScriptRef(script_key=key, version=2)


async def test_the_dub_keeps_the_original_sound_under_every_line(store):
    put(store, "picture_and_sound")
    activities = SourceAudioActivities(store)
    env = ActivityEnvironment()
    ingest = await env.run(activities.source_audio, SPEC)
    ref = _script(store)
    dub = await env.run(activities.keep_original_dub, SPEC, ref, ingest)
    assert dub.audio_key == ingest.bgm_key
    audio = store.get_bytes(dub.audio_key, produced_by="test")
    assert dub.audio_sha256 == hashlib.sha256(audio).hexdigest()
    assert dub.levels is None
    doc = DubSegmentsDocument.model_validate_json(
        store.get_bytes(dub.segments_key, produced_by="test")
    )
    assert [s.keep_original for s in doc.segments] == [True, True]
    assert [s.tts_key for s in doc.segments] == [None, None]
    script = LocalizedScript.model_validate_json(store.get_bytes(ref.script_key, produced_by="t"))
    # Captions and the "dub" say the same thing: the layout and the project accept it.
    check_one_translation(script, ref, dub, doc)

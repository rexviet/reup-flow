"""Persisted, generation-scoped inputs for testing the real project builder."""

import hashlib
from pathlib import Path

from localization.activities.review_project import ReviewProjectActivity
from localization.config import DEFAULT_CAPTION_FONT
from localization.contracts import (
    BBox,
    DubbedAudio,
    DubSegment,
    DubSegmentsDocument,
    IngestResult,
    LocalizedScript,
    LocalizedSegment,
    SubtitleCue,
    SubtitleDocument,
    SubtitleTrack,
)
from localization.storage.object_store import work_key_for
from tests.support.inputs import make_input


def project_inputs(store, tmp_path: Path, generation=1, text="Xin chào", segment_id=17):
    spec = make_input(generation=generation).model_copy(update={"delivery_mode": "review_project"})
    store.put_bytes(spec.video_key, f"source-{generation}".encode())
    snapshot = store.capture_source(spec.video_key, f"work/7001/g{generation}/r0/source.mp4")
    spec = spec.model_copy(update={"source_snapshot": snapshot})
    status = "translated" if text else "unresolved"
    reason = None if text else "unintelligible speech"
    script = LocalizedScript(
        aweme_id=spec.aweme_id,
        version=2,
        prompt_version="test",
        segments=(
            LocalizedSegment(
                id=segment_id,
                start_ms=0,
                end_ms=1000,
                slot_ms=1200,
                src_zh="你好",
                text_vi=text,
                status=status,
                reason=reason,
                on_screen=True,
            ),
        ),
    )
    script_key = work_key_for(spec, 0, "script.v2.json")
    store.put_bytes(script_key, script.model_dump_json().encode())
    clip_key = "cache/tts/" + "a" * 64 + ".wav" if text else None
    if clip_key:
        store.put_bytes(clip_key, b"clip")
    doc = DubSegmentsDocument(
        aweme_id=spec.aweme_id,
        script_version=2,
        segments=(
            DubSegment(
                id=segment_id,
                text_vi=text,
                status=status,
                reason=reason,
                tts_key=clip_key,
                duration_ms=800 if text else 0,
                stretch_ratio=1,
                overflow_unresolved=False,
            ),
        ),
    )
    segments_key = work_key_for(spec, 0, "dub-segments.json")
    store.put_bytes(segments_key, doc.model_dump_json().encode())
    audio_key = work_key_for(spec, 1, "dubbed.wav")
    store.put_bytes(audio_key, b"mixed-track")
    bgm_key = work_key_for(spec, 0, "bgm.wav")
    store.put_bytes(bgm_key, b"bgm")
    subs_key = work_key_for(spec, 0, "subtitles.json")
    subs = SubtitleDocument(
        width=640,
        height=360,
        region=BBox(x=20, y=290, w=600, h=40),
        cues=(SubtitleCue(start_ms=0, end_ms=1200),),
    )
    store.put_bytes(subs_key, subs.model_dump_json().encode())
    font = tmp_path / "font.ttf"
    font.write_bytes(DEFAULT_CAPTION_FONT.read_bytes())
    builder = ReviewProjectActivity(store, font_path=str(font), model="tts-model")
    dub = DubbedAudio(
        audio_key=audio_key,
        audio_sha256=hashlib.sha256(b"mixed-track").hexdigest(),
        script_key=script_key,
        script_version=2,
        segments_key=segments_key,
    )
    ingest = IngestResult(
        vocals_key=work_key_for(spec, 0, "vocals.wav"),
        bgm_key=bgm_key,
        duration_ms=2000,
        width=640,
        height=360,
        fps=25,
    )
    return builder, spec, dub, ingest, SubtitleTrack(subtitle_key=subs_key, region=subs.region)

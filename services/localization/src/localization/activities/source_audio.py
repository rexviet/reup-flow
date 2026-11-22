"""The sound of a subtitles-only run: the source's own, with no voice separated or made.

A subtitles-only run translates the burned-in subtitles and lays them over the original
picture and sound. The stages after the Localizer (the layout, the review project) are the
ones a dubbed run uses, and they read an `IngestResult` and a `DubbedAudio`. These
activities make both from the source alone:

- `source_audio` decodes the source's sound once (silence of its length when it has none)
  and stands it in for both stems: there is nothing to separate it from.
- `empty_transcript` is the transcript of a run that hears nothing: every line the
  Localizer gets is then read from the subtitles on screen (`complete_transcript`).
- `keep_original_dub` writes the segments document of a track that speaks no line: every
  segment keeps the original sound, and the track is the source's sound itself.
"""

import asyncio
import hashlib
import tempfile
from pathlib import Path

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import extract_audio, probe_video, run_blocking, run_ffmpeg
from localization.activities.ingest import PREP_ROUND, download_source
from localization.activities.persist import put_json
from localization.activities.script_document import read_script
from localization.contracts import (
    DubbedAudio,
    DubSegment,
    DubSegmentsDocument,
    IngestResult,
    LocalizationInput,
    LocalizedScriptRef,
    Transcript,
    TranscriptDocument,
)
from localization.observability.tracing import tag_session
from localization.storage.object_store import (
    ObjectStore,
    require_work_key,
    source_video_key,
    work_key_for,
)

SAMPLE_RATE = 44100
AUDIO_NAME = "source-audio.wav"


def _silence(path: Path, duration_ms: int) -> None:
    run_ffmpeg(
        [
            "-f", "lavfi",
            "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
            "-t", f"{duration_ms / 1000:.3f}",
            "-c:a", "pcm_s16le",
            str(path),
        ]
    )  # fmt: skip


def _digest(store: ObjectStore, key: str) -> str:
    with tempfile.TemporaryDirectory(prefix="source-audio-") as tmp:
        path = Path(tmp) / AUDIO_NAME
        store.download_file(key, path, produced_by="source_audio")
        with path.open("rb") as src:
            return hashlib.file_digest(src, "sha256").hexdigest()


class SourceAudioActivities:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @activity.defn(name="source_audio")
    async def source_audio(self, spec: LocalizationInput) -> IngestResult:
        tag_session(spec.aweme_id)
        with provider_errors(), tempfile.TemporaryDirectory(prefix="source-audio-") as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            await run_blocking(download_source, self._store, source_video_key(spec), video)
            info = await run_blocking(probe_video, video)
            audio = root / AUDIO_NAME
            if info.has_audio:
                await run_blocking(extract_audio, video, audio)
            else:
                await run_blocking(_silence, audio, info.duration_ms)
            key = work_key_for(spec, PREP_ROUND, AUDIO_NAME)
            await run_blocking(self._put_audio, key, audio)
        return IngestResult(
            vocals_key=key,
            bgm_key=key,
            duration_ms=info.duration_ms,
            width=info.width,
            height=info.height,
            fps=info.fps,
        )

    def _put_audio(self, key: str, path: Path) -> None:
        self._store.put_file(key, path, content_type="audio/wav")

    @activity.defn(name="empty_transcript")
    async def empty_transcript(self, spec: LocalizationInput) -> Transcript:
        tag_session(spec.aweme_id)
        key = work_key_for(spec, PREP_ROUND, "transcript-none.json")
        doc = TranscriptDocument(language=None, duration_ms=None, segments=(), words=())
        await asyncio.to_thread(put_json, self._store, key, doc)
        return Transcript(transcript_key=key, segment_count=0)

    @activity.defn(name="keep_original_dub")
    async def keep_original_dub(
        self, spec: LocalizationInput, script: LocalizedScriptRef, ingest: IngestResult
    ) -> DubbedAudio:
        tag_session(spec.aweme_id)
        require_work_key(spec, ingest.bgm_key)
        doc = await asyncio.to_thread(
            read_script, self._store, script.script_key, spec, script.version
        )
        segments = tuple(
            DubSegment(
                id=seg.id,
                text_vi=seg.text_vi,
                status=seg.status,
                reason=seg.reason,
                tts_key=None,
                duration_ms=0,
                stretch_ratio=1.0,
                overflow_unresolved=False,
                keep_original=True,
            )
            for seg in doc.segments
        )
        document = DubSegmentsDocument(
            aweme_id=spec.aweme_id, script_version=doc.version, segments=segments
        )
        key = work_key_for(spec, PREP_ROUND, "dub-segments.json")
        await asyncio.to_thread(put_json, self._store, key, document)
        digest = await asyncio.to_thread(_digest, self._store, ingest.bgm_key)
        return DubbedAudio(
            audio_key=ingest.bgm_key,
            audio_sha256=digest,
            script_key=script.script_key,
            script_version=script.version,
            segments_key=key,
        )

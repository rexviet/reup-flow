"""Which transcript segments are sung. A sung segment keeps the original sound: it is
captioned but not dubbed, and the mix carries the original vocals over it.

Each segment's own stretch of the source video's original audio (music included) is played
to an audio-capable model, twice with two different questions, and the segment is sung only
when both answers say so. The separated vocals were tried first and made a spoken line
read as sung; the original sound did not. When the source video is not stored beside the
transcript the vocals are used instead. A segment shorter than `MIN_SINGING_MS` is never
asked and stays speech: too short to tell, and the measured mistakes were all such lines.
With no model configured nothing is asked, every segment stays speech, and the result says
the check was not made.
"""

import asyncio
import hashlib
import json
import tempfile
from functools import partial
from pathlib import Path

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import run_blocking, run_ffmpeg
from localization.activities.localizer_ports import read_transcript_document
from localization.activities.persist import put_json
from localization.activities.synthesize import background_heartbeat
from localization.contracts import LocalizationInput, SingingCheck, Transcript
from localization.observability.tracing import tag_session
from localization.providers.singing import SingingDetector
from localization.storage.object_store import (
    ArtifactMissingError,
    ObjectStore,
    require_work_key,
    work_key_for,
)

PROMPT_VERSION = "singing-2"
# Lines under about two seconds were the only ones the model got wrong (0.9 s and 1.4 s).
MIN_SINGING_MS = 2000
# Clips asked about at once.
ASK_CONCURRENCY = 4
DOCUMENT_NAME = "transcript.singing.json"


def audio_clip(audio: Path, start_ms: int, end_ms: int, out: Path) -> bytes:
    """The audio of a file (video or wav) from `start_ms` to `end_ms`, as 16 kHz mono wav."""
    run_ffmpeg(
        [
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{(end_ms - start_ms) / 1000:.3f}",
            "-i",
            str(audio),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(out),
        ],
        timeout=60,
    )
    try:
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


class DetectSingingActivity:
    def __init__(self, store: ObjectStore, detector: SingingDetector | None) -> None:
        self._store = store
        self._detector = detector

    @activity.defn(name="detect_singing")
    async def detect_singing(
        self, spec: LocalizationInput, transcript: Transcript, vocals_key: str
    ) -> SingingCheck:
        tag_session(spec.aweme_id)
        detector = self._detector
        if detector is None:
            activity.logger.warning(
                "singing not checked: SINGING_MODEL is not set, every segment stays speech"
            )
            return SingingCheck(transcript=transcript, checked=False)
        require_work_key(spec, transcript.transcript_key)
        require_work_key(spec, vocals_key)
        doc = await asyncio.to_thread(
            read_transcript_document, self._store, transcript.transcript_key
        )
        asked = [s for s in doc.segments if s.end_ms - s.start_ms >= MIN_SINGING_MS]
        prefix = transcript.transcript_key.rsplit("/", 1)[0] + "/"
        singing: set[int] = set()
        if asked:
            with provider_errors(), tempfile.TemporaryDirectory(prefix="singing-") as temp:
                root = Path(temp)
                audio, audio_key = root / "source.mp4", prefix + "source.mp4"
                try:
                    await run_blocking(
                        partial(self._store.download_file, produced_by="capture_review_source"),
                        audio_key,
                        audio,
                    )
                except ArtifactMissingError:
                    # No stored source video: fall back to the separated vocals, which
                    # `ingest` produced (if they are gone it runs again).
                    audio, audio_key = root / "vocals.wav", vocals_key
                    await run_blocking(
                        partial(self._store.download_file, produced_by="ingest"),
                        audio_key,
                        audio,
                    )
                semaphore = asyncio.Semaphore(ASK_CONCURRENCY)

                async def ask(segment) -> bool:
                    identity = json.dumps(
                        [
                            PROMPT_VERSION,
                            detector.model_id,
                            audio_key,
                            segment.start_ms,
                            segment.end_ms,
                        ]
                    ).encode()
                    key = prefix + "singing/" + hashlib.sha256(identity).hexdigest() + ".json"
                    try:
                        cached = await asyncio.to_thread(
                            self._store.get_bytes, key, produced_by="detect_singing"
                        )
                    except ArtifactMissingError:
                        pass
                    else:
                        return bool(json.loads(cached)["singing"])
                    async with semaphore:
                        wav = await run_blocking(
                            audio_clip,
                            audio,
                            segment.start_ms,
                            segment.end_ms,
                            root / f"{segment.id}.wav",
                        )
                        answer = await detector.is_singing(wav)
                    await asyncio.to_thread(
                        self._store.put_immutable_bytes,
                        key,
                        json.dumps({"singing": answer}).encode(),
                    )
                    return answer

                async with background_heartbeat():
                    answers = await asyncio.gather(*(ask(s) for s in asked), return_exceptions=True)
                for answer in answers:
                    if isinstance(answer, BaseException):
                        raise answer
                singing = {s.id for s, sung in zip(asked, answers, strict=True) if sung}
        kinds = ["singing" if s.id in singing else "speech" for s in doc.segments]
        ids = tuple(sorted(singing))
        if all(s.kind == kind for s, kind in zip(doc.segments, kinds, strict=True)):
            return SingingCheck(transcript=transcript, checked=True, singing_ids=ids)
        activity.logger.info("segments %s are sung: their original sound is kept", list(ids))
        marked = doc.model_copy(
            update={
                "segments": tuple(
                    s.model_copy(update={"kind": kind})
                    for s, kind in zip(doc.segments, kinds, strict=True)
                )
            }
        )
        key = work_key_for(spec, 0, DOCUMENT_NAME)
        await asyncio.to_thread(put_json, self._store, key, marked)
        return SingingCheck(
            transcript=Transcript(transcript_key=key, segment_count=len(marked.segments)),
            checked=True,
            singing_ids=ids,
        )

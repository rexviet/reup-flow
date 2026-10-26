"""Stand-ins for the preparation activities, registered under the real names.

They model the real shape of what the real activities do to the object store:
`ingest` leaves the vocals behind, `transcribe` raises the non-retryable
`ArtifactMissingError` naming `ingest` when they are gone.
"""

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from temporalio import activity

from localization.contracts import (
    BBox,
    IngestResult,
    LocalizationInput,
    SubtitleTrack,
    Transcript,
)
from localization.storage.object_store import ArtifactMissingError, work_key

REGION = BBox(x=100, y=900, w=800, h=80)


@dataclass
class PrepFakes:
    # 1-based ingest run numbers whose vocals are deleted right after they are written.
    lose_vocals_on_ingest_runs: set[int] = field(default_factory=set)
    region: BBox | None = REGION
    delay_seconds: float = 0.0
    store: set[str] = field(default_factory=set)
    calls: Counter = field(default_factory=Counter)
    # (activity, monotonic enter, monotonic exit)
    spans: list[tuple[str, float, float]] = field(default_factory=list)

    async def _busy(self, name: str) -> None:
        entered = time.monotonic()
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        self.spans.append((name, entered, time.monotonic()))

    def activities(self) -> list[Callable]:
        fakes = self

        @activity.defn(name="ingest")
        async def ingest(spec: LocalizationInput) -> IngestResult:
            fakes.calls["ingest"] += 1
            await fakes._busy("ingest")
            vocals = work_key(spec.aweme_id, 0, "vocals.wav")
            if fakes.calls["ingest"] not in fakes.lose_vocals_on_ingest_runs:
                fakes.store.add(vocals)
            else:
                fakes.store.discard(vocals)
            return IngestResult(
                vocals_key=vocals,
                bgm_key=work_key(spec.aweme_id, 0, "bgm.wav"),
                duration_ms=3000,
                width=1080,
                height=1920,
                fps=30,
            )

        @activity.defn(name="transcribe")
        async def transcribe(spec: LocalizationInput, vocals_key: str) -> Transcript:
            fakes.calls["transcribe"] += 1
            await fakes._busy("transcribe")
            if vocals_key not in fakes.store:
                raise ArtifactMissingError(vocals_key, "ingest")
            return Transcript(
                transcript_key=work_key(spec.aweme_id, 0, "transcript.json"), segment_count=2
            )

        @activity.defn(name="detect_subtitles")
        async def detect_subtitles(spec: LocalizationInput) -> SubtitleTrack:
            fakes.calls["detect_subtitles"] += 1
            await fakes._busy("detect_subtitles")
            return SubtitleTrack(
                subtitle_key=work_key(spec.aweme_id, 0, "subtitles.json"), region=fakes.region
            )

        return [ingest, transcribe, detect_subtitles]

"""What the Dubber's tools reach on a worker: the script in the object store and the
TTS clip cache."""

import asyncio

from temporalio.exceptions import ApplicationError

from localization.activities.script_document import read_script
from localization.activities.synthesize import Synthesizer
from localization.agents.dubber import DubberDeps, SegmentFit, SpokenLine
from localization.storage.object_store import ObjectStore


class StoreDubberPorts:
    def __init__(self, store: ObjectStore, synthesizer: Synthesizer) -> None:
        self._store = store
        self._synth = synthesizer

    async def segment(self, deps: DubberDeps, segment_id: int) -> SegmentFit:
        script = await asyncio.to_thread(read_script, self._store, deps.script_key)
        seg = next(s for s in script.segments if s.id == segment_id)
        clip = await self._synth.cached(deps.voice, deps.engine, seg.text_vi)
        # The batch synthesis ran first, so the line's clip is there. This runs on the
        # `io` queue, so it must never speak: a missing clip is an error.
        if clip is None:
            raise ApplicationError(
                f"segment {segment_id} has no clip in the cache",
                type="ClipMissing",
                non_retryable=True,
            )
        return SegmentFit(
            id=seg.id,
            src_zh=seg.src_zh,
            text_vi=seg.text_vi,
            slot_ms=seg.slot_ms,
            duration_ms=clip.duration_ms,
        )

    async def speak(self, deps: DubberDeps, text_vi: str) -> SpokenLine:
        clip = await self._synth.clip(deps.voice, deps.engine, text_vi)
        return SpokenLine(duration_ms=clip.duration_ms)

"""Step 5, around the Dubber: which lines do not fit their slot, the script that
results from the Dubber's decisions, and the fit of every segment.

The Dubber (an agent) decides text and stretch; everything here is deterministic
and reads or writes only documents and clip metadata. The script it writes is a new
version: the one before it is never edited.
"""

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.persist import put_json
from localization.activities.script_document import read_script, write_script
from localization.activities.synthesize import Synthesizer, final_stretch, stretch_for
from localization.contracts import (
    DubbedScriptRef,
    DubDecisions,
    DubSegment,
    DubSegmentsDocument,
    FitReport,
    LocalizationInput,
    LocalizedScript,
    LocalizedScriptRef,
    LocalizedSegment,
    SegmentRoom,
    SegmentRooms,
)
from localization.contracts.vbee_flow import ClipDuration, ClipDurations
from localization.contracts.voice import VoiceRef
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore, work_key_for
from localization.text.glyphs import foreign_chars
from localization.text.safety import contacts_missing_from_source


def _missing_clip(text_hash: str) -> ApplicationError:
    return ApplicationError(
        f"the clip {text_hash} is not in the cache although it was synthesized",
        type="ClipMissing",
        non_retryable=True,
    )


def apply_decisions(
    script: LocalizedScript, decisions: DubDecisions
) -> tuple[LocalizedScript, list[int]]:
    """The next version of `script` with the Dubber's rewrites in it, and the ids of
    rewrites that were refused for a contact. A rewrite that names a contact (a link, a
    domain, a phone number) its source line lacks is refused and the line keeps its
    text: the Dubber reads untrusted video text too, and that is a sign of injection. A
    rewrite that holds a character the caption font cannot show is also not used and the
    line keeps its text, but that is not reported: nothing is wrong with the line that
    stays, it may only still overrun its slot, which `overflow_unresolved` already says."""
    by_id = {s.id: s for s in script.segments}
    chosen = {d.id: d for d in decisions.items}
    if len(chosen) != len(decisions.items) or not chosen.keys() <= by_id.keys():
        raise ApplicationError(
            "the Dubber's decisions name a segment twice or one that does not exist",
            type="InvalidDecisions",
            non_retryable=True,
        )
    refused: list[int] = []
    segments: list[LocalizedSegment] = []
    for seg in script.segments:
        decision = chosen.get(seg.id)
        text = seg.text_vi
        if seg.status != "unresolved" and decision is not None and decision.text_vi.strip():
            if contacts_missing_from_source(decision.text_vi, seg.src_zh):
                refused.append(seg.id)
            elif foreign_chars(decision.text_vi):
                activity.logger.info(
                    "rewrite of segment %s not used: unshowable characters", seg.id
                )
            else:
                text = decision.text_vi.strip()
        segments.append(seg.model_copy(update={"text_vi": text}))
    return (
        script.model_copy(update={"version": script.version + 1, "segments": tuple(segments)}),
        refused,
    )


class DubScriptActivities:
    def __init__(self, store: ObjectStore, synthesizer: Synthesizer) -> None:
        self._store = store
        self._synth = synthesizer

    async def _duration(self, voice: VoiceRef, engine: str | None, text_vi: str) -> int:
        clip = await self._synth.cached(voice, engine, text_vi)
        if clip is None:
            raise _missing_clip(self._synth.hash_of(voice, engine, text_vi))
        return clip.duration_ms

    @activity.defn(name="measure_fit")
    async def measure_fit(
        self, spec: LocalizationInput, script_key: str, engine: str | None
    ) -> FitReport:
        tag_session(spec.aweme_id)
        script = await asyncio.to_thread(read_script, self._store, script_key, spec)
        unfit: list[int] = []
        for seg in script.segments:
            if seg.status == "unresolved":
                continue
            duration = await self._duration(spec.voice, engine, seg.text_vi)
            if stretch_for(duration, seg.slot_ms) is None:
                unfit.append(seg.id)
        return FitReport(unfit_ids=tuple(unfit))

    @activity.defn(name="clip_durations")
    async def clip_durations(
        self, spec: LocalizationInput, script_key: str, engine: str | None
    ) -> ClipDurations:
        """How long each line's clip really is: what the voice's real speaking rate is
        measured from."""
        tag_session(spec.aweme_id)
        script = await asyncio.to_thread(read_script, self._store, script_key, spec)
        items = [
            ClipDuration(
                id=seg.id, duration_ms=await self._duration(spec.voice, engine, seg.text_vi)
            )
            for seg in script.segments
            if seg.status != "unresolved"
        ]
        return ClipDurations(items=tuple(items))

    @activity.defn(name="segment_room")
    async def segment_room(
        self, spec: LocalizationInput, script: LocalizedScriptRef, video_duration_ms: int
    ) -> SegmentRooms:
        """How much time each line has before the video ends, next to its slot: what a
        review that failed needs to tell a line a rerun can fix from one it cannot."""
        tag_session(spec.aweme_id)
        read = await asyncio.to_thread(
            read_script, self._store, script.script_key, spec, script.version
        )
        return SegmentRooms(
            items=tuple(
                SegmentRoom(
                    id=seg.id,
                    to_end_ms=max(0, video_duration_ms - seg.start_ms),
                    slot_ms=seg.slot_ms,
                )
                for seg in read.segments
            )
        )

    @activity.defn(name="write_dubbed_script")
    async def write_dubbed_script(
        self,
        spec: LocalizationInput,
        script: LocalizedScriptRef,
        decisions: DubDecisions,
        round: int = 0,
    ) -> DubbedScriptRef:
        tag_session(spec.aweme_id)
        before = await asyncio.to_thread(
            read_script, self._store, script.script_key, spec, script.version
        )
        after, refused = apply_decisions(before, decisions)
        if refused:
            # A rewrite that tried to add a contact is a sign of an injection: say so.
            activity.logger.warning("rewrites refused for segments %s", refused)
        written = await asyncio.to_thread(write_script, self._store, after, round, spec)
        return DubbedScriptRef(
            script_key=written.script_key, version=written.version, refused_ids=tuple(refused)
        )

    @activity.defn(name="commit_dub")
    async def commit_dub(
        self,
        spec: LocalizationInput,
        script: LocalizedScriptRef,
        engine: str | None,
        decisions: DubDecisions,
        round: int = 0,
    ) -> str:
        """Writes `dub-segments.json` (into the folder of `round`): for every segment,
        its clip, its length and the stretch it is played at. Returns the key."""
        tag_session(spec.aweme_id)
        doc = await asyncio.to_thread(
            read_script, self._store, script.script_key, spec, script.version
        )
        chosen = {d.id: d.stretch_ratio for d in decisions.items}
        segments: list[DubSegment] = []
        for seg in doc.segments:
            if seg.status == "unresolved":
                segments.append(
                    DubSegment(
                        id=seg.id,
                        text_vi="",
                        status=seg.status,
                        reason=seg.reason,
                        tts_key=None,
                        duration_ms=0,
                        stretch_ratio=1.0,
                        overflow_unresolved=False,
                    )
                )
                continue
            clip = await self._synth.cached(spec.voice, engine, seg.text_vi)
            if clip is None:
                raise _missing_clip(self._synth.hash_of(spec.voice, engine, seg.text_vi))
            ratio, overflow = final_stretch(clip.duration_ms, seg.slot_ms, chosen.get(seg.id))
            segments.append(
                DubSegment(
                    id=seg.id,
                    text_vi=seg.text_vi,
                    status=seg.status,
                    reason=seg.reason,
                    tts_key=clip.key,
                    duration_ms=clip.duration_ms,
                    stretch_ratio=ratio,
                    overflow_unresolved=overflow,
                )
            )
        document = DubSegmentsDocument(
            aweme_id=spec.aweme_id, script_version=doc.version, segments=tuple(segments)
        )
        key = work_key_for(spec, round, "dub-segments.json")
        await asyncio.to_thread(put_json, self._store, key, document)
        return key

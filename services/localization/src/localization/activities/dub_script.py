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
    DubDecisions,
    DubSegment,
    DubSegmentsDocument,
    FitReport,
    LocalizationInput,
    LocalizedScript,
    LocalizedScriptRef,
    LocalizedSegment,
)
from localization.contracts.voice import VoiceRef
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore, work_key
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
    rewrites that were refused. A rewrite that names a contact (a link, a domain, a
    phone number) its source line lacks is refused and the line keeps its text: the
    Dubber reads untrusted video text too."""
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
        if decision is not None and decision.text_vi.strip():
            if contacts_missing_from_source(decision.text_vi, seg.src_zh):
                refused.append(seg.id)
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
        script = await asyncio.to_thread(read_script, self._store, script_key)
        unfit: list[int] = []
        for seg in script.segments:
            duration = await self._duration(spec.voice, engine, seg.text_vi)
            if stretch_for(duration, seg.slot_ms) is None:
                unfit.append(seg.id)
        return FitReport(unfit_ids=tuple(unfit))

    @activity.defn(name="write_dubbed_script")
    async def write_dubbed_script(
        self, spec: LocalizationInput, script: LocalizedScriptRef, decisions: DubDecisions
    ) -> LocalizedScriptRef:
        tag_session(spec.aweme_id)
        before = await asyncio.to_thread(read_script, self._store, script.script_key)
        after, refused = apply_decisions(before, decisions)
        if refused:
            # A rewrite that tried to add a contact is a sign of an injection: say so.
            activity.logger.warning("rewrites refused for segments %s", refused)
        return await asyncio.to_thread(write_script, self._store, after)

    @activity.defn(name="commit_dub")
    async def commit_dub(
        self,
        spec: LocalizationInput,
        script: LocalizedScriptRef,
        engine: str | None,
        decisions: DubDecisions,
    ) -> str:
        """Writes `dub-segments.json`: for every segment, its clip, its length and
        the stretch it is played at. Returns the key."""
        tag_session(spec.aweme_id)
        doc = await asyncio.to_thread(read_script, self._store, script.script_key)
        chosen = {d.id: d.stretch_ratio for d in decisions.items}
        segments: list[DubSegment] = []
        for seg in doc.segments:
            clip = await self._synth.cached(spec.voice, engine, seg.text_vi)
            if clip is None:
                raise _missing_clip(self._synth.hash_of(spec.voice, engine, seg.text_vi))
            ratio, overflow = final_stretch(clip.duration_ms, seg.slot_ms, chosen.get(seg.id))
            segments.append(
                DubSegment(
                    id=seg.id,
                    text_vi=seg.text_vi,
                    tts_key=clip.key,
                    duration_ms=clip.duration_ms,
                    stretch_ratio=ratio,
                    overflow_unresolved=overflow,
                )
            )
        document = DubSegmentsDocument(
            aweme_id=spec.aweme_id, script_version=doc.version, segments=tuple(segments)
        )
        key = work_key(spec.aweme_id, 0, "dub-segments.json")
        await asyncio.to_thread(put_json, self._store, key, document)
        return key

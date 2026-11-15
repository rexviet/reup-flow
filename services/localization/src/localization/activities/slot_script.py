"""The Vbee dubbing flow's slots become the script document every audio and caption step
reads (`LocalizedScript`), so synthesis, the dubbed track, the layout and the review run
unchanged on it.

A slot is a segment: its span is the segment's span and its slot, its line is the text to
speak. There is no Chinese source line: Vbee hears the Chinese and answers in Vietnamese,
so `src_zh` is left empty. The checks that compare a line with its source (numbers and
names it must carry, a contact it must not add) have nothing to compare against there;
the shortener already checks every rewrite against the line Vbee heard.

The same step runs the deterministic safety check against what Vbee heard: a shortened
line that names a contact (a URL, a domain, a phone number) the heard line lacks marks the
run `needs_human`, as a translated line that adds one does in the other flow.
"""

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.localizer_ports import read_subtitle_document
from localization.activities.script_document import write_script
from localization.contracts import (
    LocalizationInput,
    LocalizedScript,
    LocalizedSegment,
    ScriptResult,
    SubtitleTrack,
)
from localization.contracts.vbee_flow import SlotPlan
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore
from localization.text.safety import contacts_missing_from_source

# What made the script's lines, recorded on the document like the Localizer's prompt version.
SLOT_SCRIPT_VERSION = "vbee-slots-1"


def build_slot_script(
    spec: LocalizationInput,
    plan: SlotPlan,
    heard: SlotPlan,
    cues: list[tuple[int, int]],
    version: int,
) -> tuple[LocalizedScript, list[int]]:
    """The script of `plan` and the ids of the lines that need a person. `heard` is the plan
    as Vbee's words were first shared out, before any line was shortened."""
    heard_by_id = {s.id: s for s in heard.slots}
    if [s.id for s in plan.slots] != list(heard_by_id) or any(
        (s.start_ms, s.end_ms) != (heard_by_id[s.id].start_ms, heard_by_id[s.id].end_ms)
        for s in plan.slots
    ):
        raise ApplicationError(
            "the slots to dub are not the slots that were heard",
            type="SlotMismatch",
            non_retryable=True,
        )
    flagged: list[int] = []
    segments: list[LocalizedSegment] = []
    for slot in plan.slots:
        if contacts_missing_from_source(slot.text, heard_by_id[slot.id].text):
            flagged.append(slot.id)
        segments.append(
            LocalizedSegment(
                id=slot.id,
                start_ms=slot.start_ms,
                end_ms=slot.end_ms,
                slot_ms=slot.slot_ms,
                src_zh="",
                text_vi=slot.text,
                on_screen=any(a < slot.end_ms and slot.start_ms < b for a, b in cues),
            )
        )
    script = LocalizedScript(
        aweme_id=spec.aweme_id,
        version=version,
        prompt_version=SLOT_SCRIPT_VERSION,
        segments=tuple(segments),
    )
    return script, flagged


class SlotScriptActivity:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @activity.defn(name="write_slot_script")
    async def write_slot_script(
        self,
        spec: LocalizationInput,
        plan: SlotPlan,
        heard: SlotPlan,
        subtitles: SubtitleTrack,
        version: int,
        round: int,
    ) -> ScriptResult:
        """Writes version `version` of the script into the folder of `round`."""
        tag_session(spec.aweme_id)
        doc = await asyncio.to_thread(read_subtitle_document, self._store, subtitles.subtitle_key)
        cues = [(c.start_ms, c.end_ms) for c in doc.cues]
        script, flagged = build_slot_script(spec, plan, heard, cues, version)
        if flagged:
            activity.logger.warning("shortened lines name a contact the heard line lacks")
        ref = await asyncio.to_thread(write_script, self._store, script, round)
        return ScriptResult(
            script=ref, needs_human=bool(flagged), flagged_segment_ids=tuple(flagged)
        )

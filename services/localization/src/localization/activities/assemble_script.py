"""Step 3, second half: the Localizer's lines become the `LocalizedScript`.

The agent returns only translated text. Timing, slot, source text and the
on-screen flag come from the transcript and subtitle documents, so the model
cannot invent them. The same step runs the deterministic safety check (no LLM): a
line that carries a URL, domain or phone number its source line lacks marks the
run `needs_human`.
"""

import asyncio

from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.localizer_ports import (
    read_subtitle_document,
    read_transcript_document,
    transcript_slots,
)
from localization.activities.persist import put_json
from localization.agents.localizer import PROMPT_VERSION
from localization.contracts import (
    LocalizationInput,
    LocalizedLines,
    LocalizedScript,
    LocalizedScriptRef,
    LocalizedSegment,
    ScriptResult,
)
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore, require_work_key, work_key_for
from localization.text.safety import contacts_missing_from_source

SCRIPT_VERSION = 1
# Why a line read from the screen is marked for review: nobody was heard saying it, so the
# text is the OCR's, which can be wrong.
SUBTITLE_REASON = "read from the on-screen subtitle where no speech was heard; check the text"


def build_script(
    spec: LocalizationInput,
    transcript,
    cues,
    lines: LocalizedLines,
    version: int = SCRIPT_VERSION,
) -> tuple[LocalizedScript, list[int]]:
    """The script and the ids of the lines that need a person. Raises a
    non-retryable error when the lines do not cover the transcript exactly: the
    workflow validates this too, and a mismatch here means a bug, not a retry."""
    by_id = {line.id: line for line in lines.lines}
    if len(by_id) != len(lines.lines):
        raise ApplicationError(
            "a segment was translated twice", type="IncompleteScript", non_retryable=True
        )
    wanted = {s.id for s in transcript.segments}
    if by_id.keys() != wanted:
        raise ApplicationError(
            "the lines do not cover the transcript's segments exactly",
            type="IncompleteScript",
            non_retryable=True,
        )
    spans = [(c.start_ms, c.end_ms) for c in cues]
    segments: list[LocalizedSegment] = []
    flagged: list[int] = []
    for seg, slot in zip(transcript.segments, transcript_slots(transcript), strict=True):
        line = by_id[seg.id]
        text = line.text_vi.strip()
        contextual = spec.delivery_mode == "review_project"
        if (not contextual and line.status != "translated") or (
            not text and not (contextual and line.status == "unresolved")
        ):
            raise ApplicationError(
                f"segment {seg.id} has no translation", type="IncompleteScript", non_retryable=True
            )
        if contacts_missing_from_source(text, seg.text):
            flagged.append(seg.id)
        status, reason = line.status, line.reason
        if contextual and seg.source == "subtitle" and status == "translated":
            # The Studio flags inferred lines for a person to check.
            status, reason = "inferred", SUBTITLE_REASON
        segments.append(
            LocalizedSegment(
                id=seg.id,
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
                slot_ms=slot,
                src_zh=seg.text,
                text_vi=text,
                status=status,
                reason=reason,
                on_screen=any(a < seg.end_ms and seg.start_ms < b for a, b in spans),
                keep_original=seg.kind == "singing",
            )
        )
    script = LocalizedScript(
        aweme_id=spec.aweme_id,
        version=version,
        prompt_version=PROMPT_VERSION,
        segments=tuple(segments),
    )
    return script, flagged


class AssembleScriptActivity:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @activity.defn(name="assemble_script")
    async def assemble_script(
        self,
        spec: LocalizationInput,
        transcript_key: str,
        subtitle_key: str,
        lines: LocalizedLines,
        round: int = 0,
        version: int = SCRIPT_VERSION,
    ) -> ScriptResult:
        """`round` and `version` are those of a translation made again after a review:
        it is written into that round's folder as the next version, and the earlier
        scripts stay as they were."""
        tag_session(spec.aweme_id)
        require_work_key(spec, transcript_key)
        require_work_key(spec, subtitle_key)
        transcript = await asyncio.to_thread(read_transcript_document, self._store, transcript_key)
        subtitles = await asyncio.to_thread(read_subtitle_document, self._store, subtitle_key)
        script, flagged = build_script(spec, transcript, subtitles.cues, lines, version)
        key = work_key_for(spec, round, f"script.v{version}.json")
        await asyncio.to_thread(put_json, self._store, key, script)
        inferred = tuple(s.id for s in script.segments if s.status == "inferred")
        unresolved = tuple(s.id for s in script.segments if s.status == "unresolved")
        return ScriptResult(
            script=LocalizedScriptRef(script_key=key, version=version),
            needs_human=bool(flagged or inferred or unresolved),
            flagged_segment_ids=tuple(flagged),
            inferred_segment_ids=inferred,
            unresolved_segment_ids=unresolved,
        )

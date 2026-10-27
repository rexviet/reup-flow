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

from localization.activities.localizer_ports import read_subtitle_document, read_transcript_document
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
from localization.storage.object_store import ObjectStore, work_key
from localization.text.safety import contacts_missing_from_source

SCRIPT_VERSION = 1


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
    by_id = {line.id: line.text_vi.strip() for line in lines.lines}
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
    for seg in transcript.segments:
        text = by_id[seg.id]
        if not text:
            raise ApplicationError(
                f"segment {seg.id} has no translation", type="IncompleteScript", non_retryable=True
            )
        if contacts_missing_from_source(text, seg.text):
            flagged.append(seg.id)
        segments.append(
            LocalizedSegment(
                id=seg.id,
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
                slot_ms=seg.end_ms - seg.start_ms,
                src_zh=seg.text,
                text_vi=text,
                on_screen=any(a < seg.end_ms and seg.start_ms < b for a, b in spans),
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
        transcript = await asyncio.to_thread(read_transcript_document, self._store, transcript_key)
        subtitles = await asyncio.to_thread(read_subtitle_document, self._store, subtitle_key)
        script, flagged = build_script(spec, transcript, subtitles.cues, lines, version)
        key = work_key(spec.aweme_id, round, f"script.v{version}.json")
        await asyncio.to_thread(put_json, self._store, key, script)
        return ScriptResult(
            script=LocalizedScriptRef(script_key=key, version=version),
            needs_human=bool(flagged),
            flagged_segment_ids=tuple(flagged),
        )

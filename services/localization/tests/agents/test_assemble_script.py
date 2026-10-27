"""Turning the Localizer's lines into the script: every segment covered exactly
once, timing and source text from the transcript, and a person asked to look
when a line carries a contact its source lacks."""

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.assemble_script import AssembleScriptActivity
from localization.contracts import (
    LocalizedLine,
    LocalizedLines,
    LocalizedScript,
    SubtitleCue,
    SubtitleDocument,
    TranscriptDocument,
)
from localization.contracts.models import SttSegment
from localization.storage.object_store import ObjectStore, work_key
from tests.support.inputs import make_input

TRANSCRIPT_KEY = work_key("7001", 0, "transcript.json")
SUBTITLE_KEY = work_key("7001", 0, "subtitles.json")
SCRIPT_KEY = work_key("7001", 0, "script.v1.json")


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


def put_video(store: ObjectStore, segments: list[SttSegment], cues=()) -> None:
    doc = TranscriptDocument(language="zh", duration_ms=10_000, segments=tuple(segments), words=())
    store.put_bytes(TRANSCRIPT_KEY, doc.model_dump_json().encode())
    subs = SubtitleDocument(
        width=1080,
        height=1920,
        region=None,
        cues=tuple(SubtitleCue(start_ms=a, end_ms=b) for a, b in cues),
    )
    store.put_bytes(SUBTITLE_KEY, subs.model_dump_json().encode())


def seg(i: int, text: str = "你好", start: int | None = None) -> SttSegment:
    start = i * 2000 if start is None else start
    return SttSegment(id=i, start_ms=start, end_ms=start + 1500, text=text)


def lines(*pairs: tuple[int, str]) -> LocalizedLines:
    return LocalizedLines(lines=tuple(LocalizedLine(id=i, text_vi=t) for i, t in pairs))


async def assemble(store: ObjectStore, given: LocalizedLines):
    act = AssembleScriptActivity(store)
    return await ActivityEnvironment().run(
        act.assemble_script, make_input(), TRANSCRIPT_KEY, SUBTITLE_KEY, given
    )


@pytest.mark.parametrize(
    "given",
    [
        lines((0, "một")),  # a segment left out
        lines((0, "một"), (1, "hai"), (2, "ba")),  # a segment invented
        lines((0, "một"), (1, "hai"), (1, "hai nữa")),  # one translated twice
        lines((0, "một"), (1, "   ")),  # blank
    ],
    ids=["missing", "invented", "duplicate", "blank"],
)
async def test_lines_that_do_not_cover_the_transcript_exactly_write_nothing(
    store: ObjectStore, given: LocalizedLines
) -> None:
    put_video(store, [seg(0), seg(1)])
    with pytest.raises(ApplicationError) as err:
        await assemble(store, given)
    assert err.value.type == "IncompleteScript"
    assert err.value.non_retryable
    assert not store.exists(SCRIPT_KEY)


async def test_the_script_is_written_under_work_with_the_documented_name(
    store: ObjectStore,
) -> None:
    put_video(store, [seg(0), seg(1)])
    result = await assemble(store, lines((0, "một"), (1, "hai")))
    assert result.script.script_key == SCRIPT_KEY
    assert result.script.version == 1
    script = LocalizedScript.model_validate_json(store.get_bytes(SCRIPT_KEY, produced_by="x"))
    assert script.version == 1 and [s.text_vi for s in script.segments] == ["một", "hai"]
    assert script.prompt_version


async def test_a_translation_made_again_after_a_review_is_the_next_version_in_its_round(
    store: ObjectStore,
) -> None:
    put_video(store, [seg(0), seg(1)])
    await assemble(store, lines((0, "một"), (1, "hai")))
    act = AssembleScriptActivity(store)
    result = await ActivityEnvironment().run(
        act.assemble_script,
        make_input(),
        TRANSCRIPT_KEY,
        SUBTITLE_KEY,
        lines((0, "một khác"), (1, "hai khác")),
        1,
        3,
    )
    key = work_key("7001", 1, "script.v3.json")
    assert (result.script.script_key, result.script.version) == (key, 3)
    again = LocalizedScript.model_validate_json(store.get_bytes(key, produced_by="x"))
    assert again.version == 3 and [s.text_vi for s in again.segments] == ["một khác", "hai khác"]
    # The first translation is exactly as it was.
    first = LocalizedScript.model_validate_json(store.get_bytes(SCRIPT_KEY, produced_by="x"))
    assert first.version == 1 and [s.text_vi for s in first.segments] == ["một", "hai"]


async def test_a_segment_is_on_screen_only_when_a_burned_in_subtitle_overlaps_it(
    store: ObjectStore,
) -> None:
    put_video(store, [seg(0), seg(1), seg(2)], cues=[(2100, 2400)])
    await assemble(store, lines((0, "một"), (1, "hai"), (2, "ba")))
    script = LocalizedScript.model_validate_json(store.get_bytes(SCRIPT_KEY, produced_by="x"))
    assert [s.on_screen for s in script.segments] == [False, True, False]


async def test_segments_are_numbered_by_position_whatever_ids_the_provider_gave(
    store: ObjectStore,
) -> None:
    """A transcript that kept the provider's own segments may start at 1 or skip
    ids. The Localizer is told to cover ids 0..n-1, so that is what they are."""
    put_video(store, [seg(5, "甲", 0), seg(9, "乙", 2000), seg(2, "丙", 4000)])
    result = await assemble(store, lines((0, "a"), (1, "b"), (2, "c")))
    script = LocalizedScript.model_validate_json(
        store.get_bytes(result.script.script_key, produced_by="x")
    )
    assert [(s.id, s.src_zh, s.text_vi) for s in script.segments] == [
        (0, "甲", "a"),
        (1, "乙", "b"),
        (2, "丙", "c"),
    ]


async def test_a_lost_transcript_is_reported_as_a_lost_artifact_of_the_stage_that_made_it(
    store: ObjectStore,
) -> None:
    act = AssembleScriptActivity(store)
    with pytest.raises(ApplicationError) as err:
        await ActivityEnvironment().run(
            act.assemble_script, make_input(), TRANSCRIPT_KEY, SUBTITLE_KEY, lines((0, "a"))
        )
    assert err.value.type == "ArtifactMissingError"
    assert err.value.details[0] == "transcribe"


async def test_a_contact_in_the_translation_but_not_the_source_marks_that_line(
    store: ObjectStore,
) -> None:
    put_video(store, [seg(0, "大家好"), seg(1, "再见")])
    result = await assemble(store, lines((0, "Chào, vào shop-xyz.vn nhé"), (1, "Tạm biệt")))
    assert result.needs_human is True
    assert result.flagged_segment_ids == (0,)


async def test_a_clean_script_does_not_ask_for_a_person(store: ObjectStore) -> None:
    put_video(store, [seg(0, "大家好"), seg(1, "再见")])
    result = await assemble(store, lines((0, "Chào mọi người"), (1, "Tạm biệt")))
    assert result.needs_human is False
    assert result.flagged_segment_ids == ()


async def test_the_stored_slot_extends_through_the_silence_that_follows(
    store: ObjectStore,
) -> None:
    put_video(store, [seg(0, "甲", 0), seg(1, "乙", 2500), seg(2, "丙", 8000)])
    result = await assemble(store, lines((0, "a"), (1, "b"), (2, "c")))
    script = LocalizedScript.model_validate_json(
        store.get_bytes(result.script.script_key, produced_by="x")
    )
    # Each segment is 1500 ms long. The first has 1000 ms of quiet before the next; the
    # second's would be 4 s, capped at 1500 past its end; the last has only 500 ms of
    # video left after its end.
    assert [s.slot_ms for s in script.segments] == [2500, 3000, 2000]


async def test_the_slot_the_model_is_shown_is_the_slot_the_script_stores(
    store: ObjectStore,
) -> None:
    from localization.activities.glossary import GlossaryStore
    from localization.activities.localizer_ports import StorePorts

    put_video(store, [seg(0, "甲", 0), seg(1, "乙", 2500), seg(2, "丙", 8000)])
    shown = [s.slot_ms for s in await StorePorts(store, GlossaryStore("")).segments(TRANSCRIPT_KEY)]
    result = await assemble(store, lines((0, "a"), (1, "b"), (2, "c")))
    script = LocalizedScript.model_validate_json(
        store.get_bytes(result.script.script_key, produced_by="x")
    )
    assert shown == [s.slot_ms for s in script.segments] == [2500, 3000, 2000]

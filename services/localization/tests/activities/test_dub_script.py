"""The script after the Dubber and the fit of every segment: the earlier version is
never edited, a rewrite cannot smuggle in a contact, and `dub-segments.json` says how
each segment is played."""

import boto3
import pytest
from moto import mock_aws
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.dub_script import DubScriptActivities
from localization.activities.script_document import read_script, script_key
from localization.activities.synthesize import Synthesizer
from localization.contracts import (
    DubDecision,
    DubDecisions,
    DubSegmentsDocument,
    LocalizedScriptRef,
)
from localization.storage.object_store import ObjectStore, work_key
from tests.support.inputs import make_input
from tests.support.tts_fakes import FakeTts, put_script, segment

SPEC = make_input()
ENGINE = "eng"
# 0.1 s a character: 10 characters speak for 1 s.
SPEED = 0.1


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


@pytest.fixture
def tts() -> FakeTts:
    return FakeTts(seconds_per_char=SPEED)


def make(store: ObjectStore, tts: FakeTts):
    synth = Synthesizer(store, tts, model="tts-1")
    return DubScriptActivities(store, synth), synth


async def speak_all(synth: Synthesizer, store: ObjectStore, key: str) -> None:
    for seg in read_script(store, key).segments:
        await synth.clip(SPEC.voice, ENGINE, seg.text_vi)


def decisions(*items: tuple[int, str, float]) -> DubDecisions:
    return DubDecisions(
        items=tuple(DubDecision(id=i, text_vi=t, stretch_ratio=r) for i, t, r in items)
    )


REF1 = LocalizedScriptRef(script_key=script_key("7001", 1), version=1)


# --- how much time each line has ------------------------------------------------


async def test_each_line_is_given_its_time_to_the_end_of_the_video_and_its_slot(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    key = put_script(
        store,
        [
            segment(0, "a", slot_ms=3000, start_ms=1000),
            segment(1, "b", slot_ms=1000, start_ms=10_731),
        ],
    )
    ref = LocalizedScriptRef(script_key=key, version=1)
    rooms = await ActivityEnvironment().run(acts.segment_room, SPEC, ref, 11_133)
    assert [(r.id, r.to_end_ms, r.slot_ms) for r in rooms.items] == [
        (0, 10_133, 3000),
        (1, 402, 1000),
    ]


async def test_a_line_that_starts_after_the_video_ends_has_no_time_rather_than_a_negative_one(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    key = put_script(store, [segment(0, "a", slot_ms=1000, start_ms=5000)])
    ref = LocalizedScriptRef(script_key=key, version=1)
    rooms = await ActivityEnvironment().run(acts.segment_room, SPEC, ref, 4000)
    assert rooms.items[0].to_end_ms == 0


# --- which lines do not fit ----------------------------------------------------


async def test_only_the_lines_that_cannot_fit_even_when_sped_up_are_reported(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, synth = make(store, tts)
    key = put_script(
        store,
        [
            segment(0, "x" * 8, slot_ms=1000),  # 0.8 s in 1 s: fits
            segment(1, "x" * 11, slot_ms=1000),  # 1.1 s: fits at 1.1x
            segment(2, "x" * 12, slot_ms=1000),  # 1.2 s: does not fit
            segment(3, "x" * 30, slot_ms=1000),  # 3 s: does not fit
        ],
    )
    await speak_all(synth, store, key)
    report = await ActivityEnvironment().run(acts.measure_fit, SPEC, key, ENGINE)
    assert report.unfit_ids == (2, 3)


async def test_measuring_the_fit_never_calls_the_provider(store: ObjectStore, tts: FakeTts) -> None:
    acts, synth = make(store, tts)
    key = put_script(store, [segment(0, "x" * 30, slot_ms=1000)])
    await speak_all(synth, store, key)
    before = tts.total
    await ActivityEnvironment().run(acts.measure_fit, SPEC, key, ENGINE)
    assert tts.total == before


async def test_a_line_whose_clip_is_missing_fails_instead_of_passing_as_fitting(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    key = put_script(store, [segment(0, "xin chào")])
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(acts.measure_fit, SPEC, key, ENGINE)
    assert failure.value.type == "ClipMissing"
    assert failure.value.non_retryable


# --- the script after the Dubber ------------------------------------------------


async def test_the_dubbed_script_is_a_new_version_and_the_old_one_is_untouched(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    key = put_script(store, [segment(0, "câu dài dòng quá"), segment(1, "ngắn")])
    before = store.get_bytes(key, produced_by="localize")
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script, SPEC, REF1, decisions((0, "câu gọn", 1.1))
    )
    assert ref.version == 2
    assert ref.script_key == script_key("7001", 2)
    assert store.get_bytes(key, produced_by="localize") == before
    new = read_script(store, ref.script_key)
    assert new.version == 2
    assert [s.text_vi for s in new.segments] == ["câu gọn", "ngắn"]


async def test_a_segment_the_dubber_left_alone_keeps_everything_but_the_version(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    key = put_script(store, [segment(0, "một", slot_ms=3000), segment(1, "hai")])
    ref = await ActivityEnvironment().run(acts.write_dubbed_script, SPEC, REF1, decisions())
    old, new = read_script(store, key), read_script(store, ref.script_key)
    assert new.segments == old.segments
    assert new.version == old.version + 1


async def test_a_rewrite_that_adds_a_link_is_refused_and_the_line_keeps_its_text(
    store: ObjectStore, tts: FakeTts
) -> None:
    """The Dubber reads untrusted video text too; what it writes is screened like
    the Localizer's lines."""
    acts, _ = make(store, tts)
    put_script(store, [segment(0, "câu gốc"), segment(1, "câu hai")])
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script,
        SPEC,
        REF1,
        decisions((0, "ghé shop-xyz.vn nhé", 1.1), (1, "câu hai gọn", 1.0)),
    )
    new = read_script(store, ref.script_key)
    assert [s.text_vi for s in new.segments] == ["câu gốc", "câu hai gọn"]


async def test_a_rewrite_with_a_character_the_caption_font_lacks_is_not_used_and_not_reported(
    store: ObjectStore, tts: FakeTts
) -> None:
    """The line keeps its earlier, safe text. Nothing is flagged: a person is asked to look
    at a run for a contact the source lacks, not for a symbol the font cannot draw."""
    acts, _ = make(store, tts)
    put_script(store, [segment(0, "câu gốc"), segment(1, "câu hai"), segment(2, "câu ba")])
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script,
        SPEC,
        REF1,
        decisions((0, "câu gọn 你好", 1.1), (1, "câu hai gọn", 1.0), (2, "ba ✓", 1.0)),
    )
    new = read_script(store, ref.script_key)
    assert [s.text_vi for s in new.segments] == ["câu gốc", "câu hai gọn", "câu ba"]
    assert ref.refused_ids == ()


async def test_a_rewrite_with_a_contact_is_reported_even_next_to_one_with_a_glyph(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    put_script(store, [segment(0, "câu gốc"), segment(1, "câu hai")])
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script,
        SPEC,
        REF1,
        decisions((0, "ghé shop-xyz.vn nhé", 1.1), (1, "hai ✓", 1.0)),
    )
    assert ref.refused_ids == (0,)


async def test_a_rewrite_with_a_degree_sign_is_used(store: ObjectStore, tts: FakeTts) -> None:
    acts, _ = make(store, tts)
    put_script(store, [segment(0, "câu gốc")])
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script, SPEC, REF1, decisions((0, "trời 30°C", 1.0))
    )
    assert read_script(store, ref.script_key).segments[0].text_vi == "trời 30°C"
    assert ref.refused_ids == ()


async def test_a_script_with_no_refused_rewrite_says_so(store: ObjectStore, tts: FakeTts) -> None:
    acts, _ = make(store, tts)
    put_script(store, [segment(0, "câu gốc")])
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script, SPEC, REF1, decisions((0, "câu gọn", 1.0))
    )
    assert ref.refused_ids == ()


async def test_a_dubbed_script_made_after_a_review_goes_into_that_rounds_folder(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    put_script(store, [segment(0, "câu gốc")])
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script, SPEC, REF1, decisions((0, "câu gọn", 1.0)), 1
    )
    assert ref.script_key == work_key("7001", 1, "script.v2.json")
    assert read_script(store, ref.script_key).version == 2
    # The first pass's folder holds nothing new.
    assert not store.exists(script_key("7001", 2))


async def test_the_dub_document_of_a_later_round_is_written_into_its_folder(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, synth = make(store, tts)
    key = put_script(store, [segment(0, "một"), segment(1, "hai")])
    await speak_all(synth, store, key)
    out = await ActivityEnvironment().run(acts.commit_dub, SPEC, REF1, ENGINE, decisions(), 2)
    assert out == work_key("7001", 2, "dub-segments.json")
    assert not store.exists(work_key("7001", 0, "dub-segments.json"))


async def test_a_rewrite_may_keep_a_contact_the_source_line_has(
    store: ObjectStore, tts: FakeTts
) -> None:
    acts, _ = make(store, tts)
    seg = segment(0, "xem shop-xyz.vn nhé").model_copy(update={"src_zh": "看 shop-xyz.vn"})
    put_script(store, [seg])
    ref = await ActivityEnvironment().run(
        acts.write_dubbed_script, SPEC, REF1, decisions((0, "vào shop-xyz.vn", 1.0))
    )
    assert read_script(store, ref.script_key).segments[0].text_vi == "vào shop-xyz.vn"


@pytest.mark.parametrize(
    "bad",
    [decisions((9, "không có", 1.0)), decisions((0, "a", 1.0), (0, "b", 1.0))],
    ids=["unknown id", "same id twice"],
)
async def test_decisions_that_do_not_fit_the_script_write_nothing(
    store: ObjectStore, tts: FakeTts, bad: DubDecisions
) -> None:
    acts, _ = make(store, tts)
    put_script(store, [segment(0, "một")])
    with pytest.raises(ApplicationError) as failure:
        await ActivityEnvironment().run(acts.write_dubbed_script, SPEC, REF1, bad)
    assert failure.value.type == "InvalidDecisions"
    assert not store.exists(script_key("7001", 2))


# --- the fit of every segment ---------------------------------------------------


async def committed(store, tts, texts_slots, chosen=()):
    acts, synth = make(store, tts)
    key = put_script(store, [segment(i, t, slot_ms=s) for i, (t, s) in enumerate(texts_slots)])
    await speak_all(synth, store, key)
    out = await ActivityEnvironment().run(acts.commit_dub, SPEC, REF1, ENGINE, decisions(*chosen))
    return DubSegmentsDocument.model_validate_json(store.get_bytes(out, produced_by="dub_script"))


async def test_every_segment_is_in_the_document_with_a_stretch(
    store: ObjectStore, tts: FakeTts
) -> None:
    doc = await committed(
        store,
        tts,
        [("x" * 5, 2000), ("x" * 11, 1000), ("x" * 30, 1000), ("x" * 2, 4000)],
    )
    assert [s.id for s in doc.segments] == [0, 1, 2, 3]
    assert [round(s.stretch_ratio, 2) for s in doc.segments] == [1.0, 1.1, 1.15, 1.0]
    assert [s.overflow_unresolved for s in doc.segments] == [False, False, True, False]
    assert doc.script_version == 1
    assert doc.aweme_id == "7001"


async def test_the_document_names_each_clip_and_its_measured_length(
    store: ObjectStore, tts: FakeTts
) -> None:
    doc = await committed(store, tts, [("x" * 10, 4000)])
    seg = doc.segments[0]
    assert seg.text_vi == "x" * 10
    assert store.exists(seg.tts_key)
    assert seg.duration_ms == pytest.approx(1000, abs=30)


async def test_the_stretch_the_dubber_chose_is_used_when_the_clip_fits_it(
    store: ObjectStore, tts: FakeTts
) -> None:
    doc = await committed(store, tts, [("x" * 11, 1000)], chosen=[(0, "x" * 11, 1.15)])
    assert doc.segments[0].stretch_ratio == pytest.approx(1.15)
    assert doc.segments[0].overflow_unresolved is False


async def test_a_line_still_too_long_is_marked_even_if_the_dubber_accepted_it(
    store: ObjectStore, tts: FakeTts
) -> None:
    doc = await committed(store, tts, [("x" * 30, 1000)], chosen=[(0, "x" * 30, 1.15)])
    assert doc.segments[0].stretch_ratio == pytest.approx(1.15)
    assert doc.segments[0].overflow_unresolved is True


async def test_the_document_is_written_once_under_the_run_folder(
    store: ObjectStore, tts: FakeTts
) -> None:
    await committed(store, tts, [("x" * 5, 2000)])
    assert store.exists(work_key("7001", 0, "dub-segments.json"))


async def test_omission_skips_fit_and_commit_cache_and_preserves_metadata(store, tts):
    omitted = segment(0, "").model_copy(
        update={"status": "unresolved", "reason": "No recoverable speech"}
    )
    inferred = segment(1, "xin chào", slot_ms=3000).model_copy(
        update={"status": "inferred", "reason": "Supported by preceding greeting"}
    )
    key = put_script(store, [omitted, inferred])
    acts, synth = make(store, tts)
    await synth.clip(SPEC.voice, ENGINE, inferred.text_vi)
    env = ActivityEnvironment()
    assert not (await env.run(acts.measure_fit, SPEC, key, ENGINE)).unfit_ids
    rewritten = await env.run(
        acts.write_dubbed_script, SPEC, REF1, decisions((0, "invented", 1.0), (1, "chào", 1.0))
    )
    doc = read_script(store, rewritten.script_key)
    assert doc.segments[0] == omitted
    assert doc.segments[1].status == "inferred" and doc.segments[1].reason == inferred.reason
    await synth.clip(SPEC.voice, ENGINE, "chào")
    result = await env.run(acts.commit_dub, SPEC, rewritten, ENGINE, decisions())
    committed = DubSegmentsDocument.model_validate_json(store.get_bytes(result, produced_by="test"))
    row = committed.segments[0]
    assert row.status == "unresolved" and row.reason == omitted.reason
    assert row.tts_key is None and row.duration_ms == 0 and not row.overflow_unresolved
    assert committed.segments[1].reason == inferred.reason
    assert tts.total == 2


async def test_a_sung_line_is_never_spoken_but_keeps_its_text_for_the_caption(store, tts):
    sung = segment(0, "x" * 40, slot_ms=1000).model_copy(update={"keep_original": True})
    spoken = segment(1, "x" * 5, slot_ms=2000)
    key = put_script(store, [sung, spoken])
    acts, synth = make(store, tts)
    await synth.clip(SPEC.voice, ENGINE, spoken.text_vi)
    env = ActivityEnvironment()
    # Far too long for its slot, yet not the Dubber's to shorten: it is not read.
    assert (await env.run(acts.measure_fit, SPEC, key, ENGINE)).unfit_ids == ()
    durations = await env.run(acts.clip_durations, SPEC, key, ENGINE)
    assert [d.id for d in durations.items] == [1]
    out = await env.run(acts.commit_dub, SPEC, REF1, ENGINE, decisions())
    doc = DubSegmentsDocument.model_validate_json(store.get_bytes(out, produced_by="test"))
    row = doc.segments[0]
    assert row.keep_original and row.text_vi == "x" * 40 and row.status == "translated"
    assert row.tts_key is None and row.duration_ms == 0 and row.stretch_ratio == 1.0
    assert doc.segments[1].tts_key is not None and not doc.segments[1].keep_original
    assert tts.total == 1

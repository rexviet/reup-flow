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

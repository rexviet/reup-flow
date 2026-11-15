"""The review of a Vbee dub: the checks in code, on what the render is made from, and no
call to Jev (there is no source line to judge a line's meaning against). The loudness is
measured on the dubbed track."""

import inspect
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.review_render import ReviewRenderActivity
from localization.contracts import (
    DubbedAudio,
    EditSpecRef,
    LocalizedScript,
    LocalizedScriptRef,
    ReviewVerdict,
)
from localization.storage.object_store import ObjectStore
from tests.activities.test_review_before_render import TRACK_KEY
from tests.activities.test_review_before_render import loudness as _loudness
from tests.activities.test_review_render import SCRIPT_KEY, SEGMENTS_KEY, SPEC, SPEC_KEY
from tests.activities.test_review_render import store as _rendered_store
from tests.activities.test_review_render_jev import FakeJev, activity, rewrite_dub

store = _rendered_store
loudness = _loudness


async def review(act: ReviewRenderActivity, audio_key: str = TRACK_KEY) -> ReviewVerdict:
    return await ActivityEnvironment().run(
        act.review_dub,
        SPEC,
        LocalizedScriptRef(script_key=SCRIPT_KEY, version=3),
        DubbedAudio(
            audio_key=audio_key,
            audio_sha256="a" * 64,
            script_key=SCRIPT_KEY,
            script_version=3,
            segments_key=SEGMENTS_KEY,
        ),
        EditSpecRef(
            spec_key=SPEC_KEY, spec_sha256="c" * 64, script_version=3, audio_sha256="a" * 64
        ),
        0,
    )


def without_source(store: ObjectStore, **lines: str) -> None:
    """The script as the Vbee flow writes it: no source line, and `lines` (by id) changed."""
    script = LocalizedScript.model_validate_json(store.get_bytes(SCRIPT_KEY, produced_by="t"))
    segments = tuple(
        s.model_copy(update={"src_zh": "", "text_vi": lines.get(str(s.id), s.text_vi)})
        for s in script.segments
    )
    store.put_bytes(
        SCRIPT_KEY, script.model_copy(update={"segments": segments}).model_dump_json().encode()
    )


async def test_a_dub_that_fits_passes_without_asking_jev(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    without_source(store)
    jev = FakeJev()
    verdict = await review(activity(store, jev, loudness[0]))
    assert verdict.passed is True and verdict.issues == ()
    assert jev.calls == []


async def test_a_line_that_overruns_its_slot_is_a_timing_issue_of_that_line(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    without_source(store)
    rewrite_dub(store, 1, overflow_unresolved=True, duration_ms=1500)
    jev = FakeJev()
    verdict = await review(activity(store, jev, loudness[0]))
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("dub", 1)
    assert verdict.scores.timing < 1.0
    assert jev.calls == []


async def test_the_loudness_is_measured_on_the_dubbed_track(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    without_source(store)
    track, rendered = loudness
    off = await review(activity(store, FakeJev(), rendered))
    (issue,) = off.issues
    assert (issue.stage, issue.segment_id) == ("render", None) and "LUFS" in issue.reason
    assert (await review(activity(store, FakeJev(), track))).passed is True


async def test_with_no_source_only_what_the_line_itself_shows_is_checked(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    """A line left in Chinese is caught; a line is never faulted for a number or a name of
    a source it does not have."""
    without_source(store, **{"0": "你好 các bạn", "2": "mua iPhone 15 ngay"})
    verdict = await review(activity(store, FakeJev(), loudness[0]))
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("localize", 0)
    assert "not Vietnamese" in issue.reason


async def test_a_dubbed_track_that_is_gone_is_a_lost_artifact(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    with pytest.raises(ApplicationError) as info:
        await review(activity(store, FakeJev(), loudness[0]), "work/7001/r1/gone.wav")
    assert info.value.type == "ArtifactMissingError"


async def test_the_local_copy_of_the_track_is_removed_when_the_review_ends(
    store: ObjectStore, loudness: tuple[float, float], tmp_path: Path, monkeypatch
) -> None:
    # A temporary directory of its own: other test runs on this machine make `review-*` too.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    await review(activity(store, FakeJev(), loudness[0]))
    assert list(scratch.glob("review-*")) == []


def test_the_dub_review_is_its_own_activity_with_the_review_before_the_render_arguments() -> None:
    definition = ReviewRenderActivity.review_dub.__temporal_activity_definition  # type: ignore[attr-defined]
    assert definition.name == "review_dub"
    signature = inspect.signature(ReviewRenderActivity.review_dub)
    assert list(signature.parameters) == ["self", "spec", "script", "dub", "spec_ref", "round"]
    assert signature.return_annotation == ReviewVerdict

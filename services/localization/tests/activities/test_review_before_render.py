"""The review made before the render: the same checks as the `jev` review of a render,
read from what the render is made from. The loudness is measured on the dubbed track,
which is the audio the render puts in the video; no render exists yet."""

import inspect
import subprocess
import tempfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.assemble_audio import measure_loudness
from localization.activities.review_render import ReviewRenderActivity
from localization.contracts import (
    DubbedAudio,
    EditSpecRef,
    LocalizedScriptRef,
    ReviewVerdict,
)
from localization.storage.object_store import ObjectStore
from tests.activities.test_review_render import (  # noqa: F401
    RENDER_KEY,
    SCRIPT_KEY,
    SEGMENTS_KEY,
    SPEC,
    SPEC_KEY,
    Ocr,
)
from tests.activities.test_review_render import store as _rendered_store
from tests.activities.test_review_render_jev import FakeJev, activity

store = _rendered_store

TRACK_KEY = "work/7001/r1/dubbed.wav"
TRUE_PEAK = -1.5


def _loudness(path: Path) -> float:
    return float(measure_loudness(path, -14.0, TRUE_PEAK)["input_i"])


@pytest.fixture
def loudness(store: ObjectStore, tmp_path: Path) -> tuple[float, float]:
    """(the dubbed track's, the render's): a quiet track beside a loud render, so which
    one was measured shows in the verdict. The render is then removed from the store: a
    review that reads it fails."""
    track = tmp_path / "dubbed.wav"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440"]
        + ["-t", "3", "-af", "volume=0.02", str(track)],
        check=True,
    )
    store.put_file(TRACK_KEY, track)
    video = tmp_path / "probe.mp4"
    video.write_bytes(store.get_bytes(RENDER_KEY, produced_by="t"))
    measured = (_loudness(track), _loudness(video))
    (tmp_path / "bucket" / "objects" / RENDER_KEY).unlink()
    assert abs(measured[0] - measured[1]) > 10, measured
    return measured


async def review(act: ReviewRenderActivity, audio_key: str = TRACK_KEY):  # noqa: ANN201
    return await ActivityEnvironment().run(
        act.review_before_render,
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
        1,
    )


async def test_a_faithful_script_passes_without_a_render(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    jev = FakeJev()
    verdict = await review(activity(store, jev, loudness[0]))
    assert verdict is not None and verdict.passed is True and verdict.issues == ()
    assert len(jev.calls) == 1


async def test_the_loudness_is_measured_on_the_dubbed_track(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    """Aimed at the render's loudness, the quiet track is far off; aimed at its own, it
    passes. Either answer alone could be a coincidence; together they name the file."""
    track, rendered = loudness
    off = await review(activity(store, FakeJev(), rendered))
    assert off is not None
    (issue,) = off.issues
    assert (issue.stage, issue.segment_id) == ("render", None) and "LUFS" in issue.reason
    on = await review(activity(store, FakeJev(), track))
    assert on is not None and on.passed is True


async def test_the_same_checks_run_as_on_a_render(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    from tests.activities.test_review_render_jev import rewrite_dub

    rewrite_dub(store, 1, overflow_unresolved=True, duration_ms=1500)
    verdict = await review(activity(store, FakeJev(), loudness[0]))
    assert verdict is not None
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("dub", 1)


async def test_a_dubbed_track_that_is_gone_is_a_lost_artifact(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    with pytest.raises(ApplicationError) as info:
        await review(activity(store, FakeJev(), loudness[0]), "work/7001/r1/gone.wav")
    assert info.value.type == "ArtifactMissingError"
    assert "assemble_audio" in str(info.value)


async def test_the_local_copy_of_the_track_is_removed_when_the_review_ends(
    store: ObjectStore, loudness: tuple[float, float]
) -> None:
    before = {p.name for p in Path(tempfile.gettempdir()).glob("review-*")}
    await review(activity(store, FakeJev(), loudness[0]))
    assert {p.name for p in Path(tempfile.gettempdir()).glob("review-*")} == before


async def test_a_reviewer_that_looks_at_frames_answers_that_it_needs_the_render(
    store: ObjectStore,
) -> None:
    def no_model():  # noqa: ANN202
        raise AssertionError("asked before the render, the vision reviewer is not run")

    act = ReviewRenderActivity(
        store, Ocr(), no_model, loudness_lufs=-14.0, true_peak_dbtp=-1.5, mode="llm"
    )
    assert await review(act) is None


def test_the_review_before_the_render_is_its_own_activity() -> None:
    """Runs recorded before it scheduled `review_render` with a render; that activity
    keeps its name and arguments, and this one takes none of a render."""
    definition = ReviewRenderActivity.review_before_render.__temporal_activity_definition  # type: ignore[attr-defined]
    assert definition.name == "review_before_render"
    signature = inspect.signature(ReviewRenderActivity.review_before_render)
    assert list(signature.parameters) == ["self", "spec", "script", "dub", "spec_ref", "round"]
    assert signature.return_annotation == ReviewVerdict | None

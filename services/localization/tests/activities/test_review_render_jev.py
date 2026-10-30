"""The review in `jev` mode, on a real mp4: checks in code, then one call to Jev for what
needs understanding. Jev is a stand-in with the real call's shape (a `JevResult`, errors as
the provider raises them) for the verdict tests; the recording and cost test goes through
the real provider over a transport that sends OpenRouter's real body."""

import dataclasses
import json
import tempfile
from pathlib import Path

import httpx2
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.assemble_audio import measure_loudness
from localization.activities.detect_subtitles import subtitles_key
from localization.activities.review_render import JEV_STAGE, ReviewRenderActivity
from localization.agents import review_jev
from localization.contracts import (
    BBox,
    DubbedAudio,
    DubSegment,
    DubSegmentsDocument,
    EditSpec,
    EditSpecRef,
    LocalizedScript,
    LocalizedScriptRef,
    LocalizedSegment,
    RenderResult,
)
from localization.observability.llm_usage import LlmUsage, RunContext
from localization.providers.jev import Jev as RealJev
from localization.providers.jev import JevResult, make_client
from localization.storage.object_store import ObjectStore
from tests.activities.test_review_render import (  # noqa: F401
    RENDER_KEY,
    SCRIPT_KEY,
    SEGMENTS_KEY,
    SPEC,
    SPEC_KEY,
    Ocr,
    put_subtitles,
)
from tests.activities.test_review_render import store as _rendered_store

# The fixture of the vision review's tests: a store holding a real mp4 and its documents.
store = _rendered_store

TRUE_PEAK = -1.5


@dataclasses.dataclass
class FakeJev:
    """Answers each question with `base`, except the ones named in `answers`."""

    base: float = 0.05
    answers: dict[str, float] = dataclasses.field(default_factory=dict)
    fail: Exception | None = None
    calls: list[tuple[dict, dict]] = dataclasses.field(default_factory=list)

    async def ask(self, state, questions, on_usage=None):  # noqa: ANN001, ANN201
        self.calls.append((state, dict(questions)))
        if self.fail is not None:
            raise self.fail
        return JevResult(
            model="typesafe/jev-1.13-20260917",
            answers={n: self.answers.get(n, self.base) for n in questions},
            input_tokens=300,
            output_tokens=40,
            latency_ms=500,
        )


@pytest.fixture
def target_lufs(store: ObjectStore, tmp_path: Path) -> float:
    """The loudness of the test video itself, so the loudness check passes on it."""
    video = tmp_path / "probe.mp4"
    video.write_bytes(store.get_bytes(RENDER_KEY, produced_by="t"))
    return float(measure_loudness(video, -14.0, TRUE_PEAK)["input_i"])


def activity(  # noqa: ANN201
    store: ObjectStore,
    jev: FakeJev,
    target: float,
    ocr: Ocr | None = None,
    *,
    recorder=None,  # noqa: ANN001
):
    def no_model():  # noqa: ANN202
        raise AssertionError("the jev review never asks for the vision model")

    return ReviewRenderActivity(
        store,
        ocr or Ocr(),
        no_model,
        loudness_lufs=target,
        true_peak_dbtp=0.0,
        mode="jev",
        jev=lambda: jev,  # type: ignore[arg-type,return-value]
        recorder=recorder,
    )


async def review(act: ReviewRenderActivity, env: ActivityEnvironment | None = None):  # noqa: ANN201
    return await (env or ActivityEnvironment()).run(
        act.review_render,
        SPEC,
        RenderResult(render_key=RENDER_KEY, spec_sha256="c" * 64),
        LocalizedScriptRef(script_key=SCRIPT_KEY, version=3),
        DubbedAudio(
            audio_key="work/7001/r1/dubbed.wav",
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


def rewrite_script(store: ObjectStore, **changes: dict) -> None:
    """Change lines of the stored script: id -> fields of that segment."""
    script = LocalizedScript.model_validate_json(store.get_bytes(SCRIPT_KEY, produced_by="t"))
    segments = tuple(s.model_copy(update=changes.get(str(s.id), {})) for s in script.segments)
    store.put_bytes(
        SCRIPT_KEY, script.model_copy(update={"segments": segments}).model_dump_json().encode()
    )


def rewrite_dub(store: ObjectStore, ident: int, **changes: object) -> None:
    doc = DubSegmentsDocument.model_validate_json(store.get_bytes(SEGMENTS_KEY, produced_by="t"))
    rows = tuple(s.model_copy(update=changes) if s.id == ident else s for s in doc.segments)
    store.put_bytes(
        SEGMENTS_KEY, doc.model_copy(update={"segments": rows}).model_dump_json().encode()
    )


# --- the verdict --------------------------------------------------------------------------


async def test_a_faithful_render_passes_with_one_call_that_carries_only_text(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    jev = FakeJev()
    verdict = await review(activity(store, jev, target_lufs))
    assert verdict.passed is True and verdict.issues == ()
    assert (verdict.scores.fidelity, verdict.scores.timing, verdict.scores.visual) == (
        1.0,
        1.0,
        1.0,
    )
    ((state, questions),) = jev.calls
    assert len(questions) == 3 * 3
    # Text only: the lines, never frames or paths of the video.
    assert [s["target_vi"] for s in state["segments"]] == ["xin chào 0", "xin chào 1", "xin chào 2"]
    assert "jpg" not in json.dumps(state) and "render" not in json.dumps(state)


async def test_a_line_that_loses_a_name_comes_back_as_a_translation_issue_of_that_line(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    jev = FakeJev(answers={review_jev.question_name(1, "omits_content"): 0.96})
    verdict = await review(activity(store, jev, target_lufs))
    assert verdict.passed is False
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("localize", 1)
    assert "leaves out a name, number or call to action" in issue.reason
    assert "96%" in issue.reason and "xin chào 1" in issue.reason and "你好" in issue.reason
    assert verdict.scores.fidelity == 0.75 and verdict.scores.timing == 1.0


async def test_the_threshold_is_ours_to_set_just_under_passes_and_at_it_fails(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    name = review_jev.question_name(0, "changes_meaning")
    under = FakeJev(answers={name: review_jev.THRESHOLD - 0.01})
    assert (await review(activity(store, under, target_lufs))).passed is True
    at = FakeJev(answers={name: review_jev.THRESHOLD})
    assert (await review(activity(store, at, target_lufs))).passed is False


async def test_a_line_the_cheap_checks_already_failed_is_not_asked_about(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    rewrite_script(store, **{"2": {"text_vi": "Ủng hộ 明镜"}})
    jev = FakeJev()
    verdict = await review(activity(store, jev, target_lufs))
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("localize", 2)
    assert [s["id"] for s in jev.calls[0][0]["segments"]] == [0, 1]


async def test_a_script_of_lines_that_all_failed_makes_no_call(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    rewrite_script(store, **{str(i): {"text_vi": ""} for i in range(3)})
    jev = FakeJev()
    verdict = await review(activity(store, jev, target_lufs))
    assert jev.calls == [] and len(verdict.issues) == 3


async def test_many_lines_go_in_calls_of_a_size_where_jev_still_separates(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    n = review_jev.SEGMENTS_PER_CALL * 2 + 1
    script = LocalizedScript(
        aweme_id="7001",
        version=3,
        prompt_version="p",
        segments=tuple(
            LocalizedSegment(
                id=i,
                start_ms=0,
                end_ms=100,
                slot_ms=900,
                src_zh="你好",
                text_vi=f"xin chào {i}",
                on_screen=False,
            )
            for i in range(n)
        ),
    )
    store.put_bytes(SCRIPT_KEY, script.model_dump_json().encode())
    doc = DubSegmentsDocument(
        aweme_id="7001",
        script_version=3,
        segments=tuple(
            DubSegment(
                id=i,
                text_vi=f"xin chào {i}",
                tts_key=f"cache/tts/{i:064x}.wav",
                duration_ms=800,
                stretch_ratio=1.0,
                overflow_unresolved=False,
            )
            for i in range(n)
        ),
    )
    store.put_bytes(SEGMENTS_KEY, doc.model_dump_json().encode())
    jev = FakeJev()
    await review(activity(store, jev, target_lufs))
    sizes = [len(state["segments"]) for state, _ in jev.calls]
    assert sizes == [review_jev.SEGMENTS_PER_CALL, review_jev.SEGMENTS_PER_CALL, 1]


# --- what is measured in code -------------------------------------------------------------


async def test_a_line_still_over_its_slot_is_a_dub_issue_without_asking_jev_less(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    rewrite_dub(store, 1, overflow_unresolved=True, duration_ms=1500)
    verdict = await review(activity(store, FakeJev(), target_lufs))
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("dub", 1)
    assert verdict.scores.timing == 0.75 and verdict.scores.fidelity == 1.0


async def test_audio_far_from_the_target_is_measured_on_the_real_file(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    verdict = await review(activity(store, FakeJev(), target_lufs + 6.0))
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("render", None) and "LUFS" in issue.reason


BELOW_THE_COVER = BBox(x=40, y=445, w=190, h=30)


async def test_source_subtitle_the_cover_misses_is_found_from_what_the_detector_saw(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    put_subtitles(store, [BELOW_THE_COVER] * 6)
    ocr = Ocr(box=BBox(x=30, y=330, w=200, h=40))
    verdict = await review(activity(store, FakeJev(), target_lufs, ocr))
    (issue,) = verdict.issues
    assert (issue.stage, issue.segment_id) == ("render", None)
    assert "left-over source subtitle" in issue.reason and "y=445" in issue.reason
    assert verdict.scores.visual == 0.75
    # The render is not read again: its own caption is text next to the caption too.
    assert ocr.asked == []


async def test_text_the_render_shows_around_its_caption_is_not_left_over_subtitle(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    ocr = Ocr(box=BBox(x=30, y=330, w=200, h=40))
    assert (await review(activity(store, FakeJev(), target_lufs, ocr))).passed is True
    assert ocr.asked == []


async def test_a_video_without_a_covered_subtitle_has_nothing_left_over(
    store: ObjectStore,
    target_lufs: float,
    tmp_path: Path,
) -> None:
    spec = EditSpec.model_validate_json(store.get_bytes(SPEC_KEY, produced_by="t"))
    store.put_bytes(SPEC_KEY, spec.model_copy(update={"cover": None}).model_dump_json().encode())
    (tmp_path / "bucket" / "objects" / subtitles_key("7001")).unlink()
    assert (await review(activity(store, FakeJev(), target_lufs))).passed is True


async def test_a_covered_video_whose_subtitle_document_is_gone_cannot_be_reviewed(
    store: ObjectStore,
    target_lufs: float,
    tmp_path: Path,
) -> None:
    (tmp_path / "bucket" / "objects" / subtitles_key("7001")).unlink()
    with pytest.raises(ApplicationError) as info:
        await review(activity(store, FakeJev(), target_lufs))
    assert info.value.type == "ArtifactMissingError"
    assert "detect_subtitles" in str(info.value)


# --- failures -----------------------------------------------------------------------------


def review_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("review-*")}


@pytest.mark.parametrize(
    ("etype", "permanent"), [("TransientProviderError", False), ("PermanentProviderError", True)]
)
async def test_a_jev_failure_reaches_temporal_as_the_provider_classified_it_and_cleans_up(
    store: ObjectStore,
    target_lufs: float,
    etype: str,
    permanent: bool,
) -> None:
    before = review_dirs()
    jev = FakeJev(fail=ApplicationError("jev: HTTP 503", type=etype, non_retryable=permanent))
    with pytest.raises(ApplicationError) as info:
        await review(activity(store, jev, target_lufs))
    assert (info.value.type, info.value.non_retryable) == (etype, permanent)
    assert review_dirs() == before


def test_the_jev_mode_needs_a_client_and_the_llm_mode_needs_none(store: ObjectStore) -> None:
    with pytest.raises(ValueError, match="Jev client"):
        ReviewRenderActivity(
            store,
            Ocr(),
            lambda: None,
            loudness_lufs=-14.0,
            true_peak_dbtp=-1.5,
            mode="jev",  # type: ignore[arg-type,return-value]
        )
    ReviewRenderActivity(
        store,
        Ocr(),
        lambda: None,
        loudness_lufs=-14.0,
        true_peak_dbtp=-1.5,
        mode="llm",  # type: ignore[arg-type,return-value]
    )


# --- cost and the record ------------------------------------------------------------------


async def test_the_call_is_recorded_under_its_own_stage_with_the_cost_openrouter_reported(
    store: ObjectStore,
    target_lufs: float,
) -> None:
    recorded: list[tuple[LlmUsage, RunContext]] = []

    async def recorder(usage: LlmUsage, run: RunContext) -> None:
        recorded.append((usage, run))

    def handler(request: httpx2.Request) -> httpx2.Response:
        names = json.loads(request.content)["questions"]
        body = {
            "model": "typesafe/jev-1.13-20260917",
            "answers": {n: {"type": "noul", "noul": 0.05} for n in names},
            "usage": {"input_tokens": 420, "output_tokens": 90, "cost": 0.0000191},
            "id": "gen-dec-1793112141-abc",
            "provider": "TypeSafe",
        }
        return httpx2.Response(200, json=body)

    client = make_client(
        "k",
        base_url="https://openrouter.ai/api",
        model="typesafe/jev-1.13",
        transport=httpx2.MockTransport(handler),
    )
    real = RealJev(client, model="typesafe/jev-1.13")
    act = ReviewRenderActivity(
        store,
        Ocr(),
        lambda: None,  # type: ignore[arg-type,return-value]
        loudness_lufs=target_lufs,
        true_peak_dbtp=0.0,
        mode="jev",
        jev=lambda: real,
        recorder=recorder,
    )
    env = ActivityEnvironment()
    env.info = dataclasses.replace(
        env.info, workflow_id="video-7001-g3", activity_type="review_render"
    )
    verdict = await review(act, env)
    assert verdict.passed is True
    ((usage, run),) = recorded
    assert (run.aweme_id, run.generation, run.stage) == ("7001", 3, JEV_STAGE)
    assert usage.generation_id == "gen-dec-1793112141-abc"
    assert (usage.prompt_tokens, usage.completion_tokens) == (420, 90)
    assert usage.cost_usd == pytest.approx(0.0000191)


def test_both_modes_are_the_same_activity_to_the_workflow(store: ObjectStore) -> None:  # noqa: ARG001
    """A workflow recorded before the Jev review scheduled `review_render` with these
    arguments and read a `ReviewVerdict`; replaying it must find the same activity."""
    import inspect

    definition = ReviewRenderActivity.review_render.__temporal_activity_definition  # type: ignore[attr-defined]
    assert definition.name == "review_render"
    params = list(inspect.signature(ReviewRenderActivity.review_render).parameters)
    assert params == ["self", "spec", "render", "script", "dub", "spec_ref", "round"]
    assert inspect.signature(ReviewRenderActivity.review_render).return_annotation.__name__ == (
        "ReviewVerdict"
    )

"""A finished run leaves its scores on its own Langfuse trace.

The real `publish` activity runs inside a span (as it does under the Temporal
interceptor) over an object store on a directory; a stub HTTP server plays Langfuse's
public API and records what it is sent.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from temporalio.testing import ActivityEnvironment

from localization.activities.publish import PublishActivity
from localization.activities.script_document import write_script
from localization.contracts import (
    DubbedAudio,
    DubSegment,
    DubSegmentsDocument,
    LocalizedScript,
    LocalizedSegment,
    PublishRequest,
)
from localization.observability.scores import LangfuseScores, has_han, run_scores
from localization.storage.object_store import ObjectStore, work_key
from tests.support.file_s3 import FileS3
from tests.support.inputs import make_input
from tests.support.review_fakes import MemoryRuns, issue, verdict
from tests.support.stub_server import Scenario, StubServer

RENDER = "work/7001/render-" + "a" * 64 + ".mp4"
SEGMENTS = work_key("7001", 0, "dub-segments.json")
SPEC = make_input("7001", 3)
TRACER = TracerProvider().get_tracer("test")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def store(root: Path) -> ObjectStore:
    store = ObjectStore(FileS3(root), "reup-media")
    store.put_bytes(RENDER, b"the-mp4")
    return store


@pytest.fixture
def langfuse() -> Iterator[StubServer]:
    server = StubServer(scenario=Scenario(json_body={"id": "ok"})).start()
    yield server
    server.stop()


def _dub(store: ObjectStore, texts: list[str], stretches: list[float], overflow: list[bool]):
    script = LocalizedScript(
        aweme_id="7001",
        version=1,
        prompt_version="localizer-1",
        segments=tuple(
            LocalizedSegment(
                id=i,
                start_ms=i * 1000,
                end_ms=i * 1000 + 900,
                slot_ms=1000,
                src_zh="你好",
                text_vi=t,
                on_screen=False,
            )
            for i, t in enumerate(texts)
        ),
    )
    ref = write_script(store, script)
    doc = DubSegmentsDocument(
        aweme_id="7001",
        script_version=1,
        segments=tuple(
            DubSegment(
                id=i,
                text_vi=t,
                tts_key="cache/tts/" + "b" * 64 + ".wav",
                duration_ms=900,
                stretch_ratio=r,
                overflow_unresolved=o,
            )
            for i, (t, r, o) in enumerate(zip(texts, stretches, overflow, strict=True))
        ),
    )
    store.put_bytes(SEGMENTS, doc.model_dump_json().encode())
    return DubbedAudio(
        audio_key=work_key("7001", 0, "dub.wav"),
        audio_sha256="c" * 64,
        script_key=ref.script_key,
        script_version=1,
        segments_key=SEGMENTS,
    )


def _scorer(langfuse: StubServer) -> LangfuseScores:
    return LangfuseScores(langfuse.url, "pk-lf-test", "sk-lf-test")


async def _publish(act: PublishActivity, request: PublishRequest) -> str:
    """Publishes inside a span and returns that span's trace id."""
    with (
        TRACER.start_as_current_span("RunWorkflow:VideoLocalizationWorkflow"),
        TRACER.start_as_current_span("RunActivity:publish") as span,
    ):
        await ActivityEnvironment().run(act.publish, SPEC, request)
        return format(span.get_span_context().trace_id, "032x")


def _sent(langfuse: StubServer) -> dict[str, dict]:
    bodies = [json.loads(r.body) for r in langfuse.requests if r.path == "/api/public/scores"]
    return {b["name"]: b for b in bodies}


async def test_a_published_run_puts_every_score_on_its_own_trace(
    store: ObjectStore, langfuse: StubServer
) -> None:
    dub = _dub(store, ["xin chào", "còn 你 đây", "tạm biệt"], [1.0, 1.1, 0.9], [False, True, False])
    request = PublishRequest(
        outcome="published", render_key=RENDER, verdict=verdict(), review_rounds=1, dub=dub
    )
    trace_id = await _publish(PublishActivity(store, MemoryRuns(), _scorer(langfuse)), request)

    sent = _sent(langfuse)
    assert {k: v["value"] for k, v in sent.items()} == {
        "fidelity": 0.9,
        "timing": 0.9,
        "visual": 0.9,
        "overflow_rate": pytest.approx(1 / 3),
        "mean_stretch": pytest.approx(1.0),
        "residual_han_cues": 1,
        "review_rounds": 1,
        "needs_human": 0.0,
        "safety_flag": 0.0,
    }
    assert {v["traceId"] for v in sent.values()} == {trace_id}
    # A score that also names a session is refused by Langfuse.
    assert not any("sessionId" in v for v in sent.values())
    assert sent["needs_human"]["dataType"] == "BOOLEAN"
    assert sent["fidelity"]["dataType"] == "NUMERIC"
    auth = langfuse.requests[0].headers["authorization"]
    assert auth == "Basic cGstbGYtdGVzdDpzay1sZi10ZXN0"  # base64("pk-lf-test:sk-lf-test")


async def test_sending_again_overwrites_the_same_scores(
    store: ObjectStore, langfuse: StubServer
) -> None:
    request = PublishRequest(outcome="published", render_key=RENDER, verdict=verdict())
    act = PublishActivity(store, MemoryRuns(), _scorer(langfuse))
    with (
        TRACER.start_as_current_span("RunWorkflow:VideoLocalizationWorkflow"),
        TRACER.start_as_current_span("RunActivity:publish"),
    ):
        await ActivityEnvironment().run(act.publish, SPEC, request)
        await ActivityEnvironment().run(act.publish, SPEC, request)
    ids = [json.loads(r.body)["id"] for r in langfuse.requests]
    assert ids and len(ids) == 2 * len(set(ids))


async def test_an_activity_span_that_is_its_own_root_is_not_the_runs_trace(
    store: ObjectStore, langfuse: StubServer
) -> None:
    """A workflow started without a trace: each publish attempt is a root of its own, and
    scores sent there would miss the run and pile up on retries."""
    request = PublishRequest(outcome="published", render_key=RENDER, verdict=verdict())
    act = PublishActivity(store, MemoryRuns(), _scorer(langfuse))
    with TRACER.start_as_current_span("RunActivity:publish"):
        await ActivityEnvironment().run(act.publish, SPEC, request)
    assert langfuse.requests == []


async def test_a_scorer_that_raises_does_not_fail_the_run(store: ObjectStore) -> None:
    class Broken:
        async def send(self, trace_id: str, scores: object) -> int:
            raise RuntimeError("unexpected")

    runs = MemoryRuns()
    request = PublishRequest(outcome="published", render_key=RENDER, verdict=verdict())
    await _publish(PublishActivity(store, runs, Broken()), request)  # type: ignore[arg-type]
    assert runs.rows[("7001", 3)]["status"] == "published"


async def test_a_run_that_needs_a_person_says_so_and_why(
    store: ObjectStore, langfuse: StubServer
) -> None:
    request = PublishRequest(
        outcome="needs_human",
        render_key=RENDER,
        verdict=verdict(issue("dub", segment_id=0)),
        review_rounds=2,
        reasons=("flagged",),
        safety_flag=True,
    )
    await _publish(PublishActivity(store, MemoryRuns(), _scorer(langfuse)), request)
    sent = _sent(langfuse)
    assert (sent["needs_human"]["value"], sent["safety_flag"]["value"]) == (1.0, 1.0)
    assert sent["review_rounds"]["value"] == 2
    # No dub was named, so nothing was measured from one.
    assert "overflow_rate" not in sent and "residual_han_cues" not in sent


async def test_a_failed_run_has_no_review_scores(store: ObjectStore, langfuse: StubServer) -> None:
    request = PublishRequest(outcome="failed", reasons=("ingest failed",))
    await _publish(PublishActivity(store, MemoryRuns(), _scorer(langfuse)), request)
    assert set(_sent(langfuse)) == {"review_rounds", "needs_human", "safety_flag"}


async def test_a_missing_dub_document_costs_its_scores_not_the_run(
    root: Path, store: ObjectStore, langfuse: StubServer
) -> None:
    dub = _dub(store, ["xin chào"], [1.0], [False])
    (root / "objects" / SEGMENTS).unlink()
    runs = MemoryRuns()
    request = PublishRequest(outcome="published", render_key=RENDER, verdict=verdict(), dub=dub)
    await _publish(PublishActivity(store, runs, _scorer(langfuse)), request)
    assert runs.rows[("7001", 3)]["status"] == "published"
    assert "fidelity" in _sent(langfuse) and "overflow_rate" not in _sent(langfuse)


async def test_langfuse_down_does_not_fail_the_run(store: ObjectStore) -> None:
    down = StubServer(scenario=Scenario(status=503)).start()
    try:
        runs = MemoryRuns()
        request = PublishRequest(outcome="published", render_key=RENDER, verdict=verdict())
        await _publish(PublishActivity(store, runs, _scorer(down)), request)
        assert down.requests, "the scores were never attempted"
    finally:
        down.stop()
    unreachable = LangfuseScores("http://127.0.0.1:9", "pk", "sk", timeout=0.5)
    await _publish(PublishActivity(store, runs, unreachable), request)
    assert runs.rows[("7001", 3)]["status"] == "published"


async def test_outside_a_trace_nothing_is_sent(store: ObjectStore, langfuse: StubServer) -> None:
    request = PublishRequest(outcome="published", render_key=RENDER, verdict=verdict())
    await ActivityEnvironment().run(
        PublishActivity(store, MemoryRuns(), _scorer(langfuse)).publish, SPEC, request
    )
    assert langfuse.requests == []


def test_without_langfuse_settings_there_is_no_scorer() -> None:
    from localization.config import Settings

    assert LangfuseScores.from_settings(Settings()) is None


def test_han_is_counted_only_for_han_characters() -> None:
    assert has_han("còn 你 đây") and has_han("明镜")
    assert not has_han("xin chào, 100%! — ok") and not has_han("ｈｅｌｌｏ。")


def test_the_scores_of_a_run_without_a_review_or_a_dub() -> None:
    scores = run_scores(
        verdict=None,
        segments=None,
        script=None,
        review_rounds=0,
        needs_human=False,
        safety_flag=False,
    )
    assert [s.name for s in scores] == ["review_rounds", "needs_human", "safety_flag"]

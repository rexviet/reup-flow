"""The Reviewer: what its tools allow, what it must return, and that the frames it
looks at stay out of everything but the model's own request. The agent runs for real
on a scripted model; only the ports (frames, loudness, OCR) are stand-ins."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from localization.agents.reviewer import (
    MAX_FRAME_CALLS,
    MAX_FRAMES_PER_CALL,
    MAX_RESIDUAL_CALLS,
    Frame,
    Loudness,
    ResidualFinding,
    ReviewDeps,
    alignment_report,
    build_reviewer,
    check_verdict,
    read_script,
    residual_source_text,
    sample_frames,
)
from localization.contracts import (
    BBox,
    Cover,
    DubSegment,
    DubSegmentsDocument,
    EditSpec,
    LocalizedScript,
    LocalizedSegment,
)
from tests.support.review_fakes import issue, verdict

JPEG = b"\xff\xd8\xff" + b"\x00" * 64


def script() -> LocalizedScript:
    return LocalizedScript(
        aweme_id="7001",
        version=2,
        prompt_version="p",
        segments=tuple(
            LocalizedSegment(
                id=i,
                start_ms=i * 1000,
                end_ms=i * 1000 + 900,
                slot_ms=900,
                src_zh=f"你好{i}",
                text_vi=f"xin chào {i}",
                on_screen=False,
            )
            for i in range(3)
        ),
    )


COVER = BBox(x=100, y=1500, w=800, h=100)


def spec(cover: BBox | None = COVER) -> EditSpec:
    return EditSpec(
        source_key="work/7001/r0/source.mp4",
        audio_key="work/7001/r0/dubbed.wav",
        audio_sha256="a" * 64,
        script_version=2,
        width=1080,
        height=1920,
        fps=30,
        duration_ms=10_000,
        cover=None if cover is None else Cover(region=cover, color="#101010", opacity_pct=92),
        caption_box=BBox(x=100, y=1500, w=800, h=100),
        captions=(),
        font="f.ttf",
        font_sha256="b" * 64,
    )


@dataclass
class FakePorts:
    found: dict[int, tuple[int, int, int, int] | None] = field(default_factory=dict)
    frame_requests: list[list[int]] = field(default_factory=list)

    async def frames(self, times_ms: Sequence[int]) -> list[Frame]:
        self.frame_requests.append(list(times_ms))
        return [Frame(t, JPEG) for t in times_ms]

    async def loudness(self) -> Loudness:
        return Loudness(-14.2, -1.6)

    async def find_text(self, times_ms: Sequence[int]) -> list[ResidualFinding]:
        return [ResidualFinding(t, self.found.get(t)) for t in times_ms]


def deps(ports: FakePorts | None = None, cover: BBox | None = COVER):
    segments = DubSegmentsDocument(
        aweme_id="7001",
        script_version=2,
        segments=tuple(
            DubSegment(
                id=s.id,
                text_vi=s.text_vi,
                tts_key=f"cache/tts/{s.id:064x}.wav",
                duration_ms=1000 if s.id == 1 else 800,
                stretch_ratio=1.15 if s.id == 1 else 1.0,
                overflow_unresolved=s.id == 1,
            )
            for s in script().segments
        ),
    )
    return ReviewDeps(
        script=script(), segments=segments, spec=spec(cover), ports=ports or FakePorts()
    )


def ctx(d: ReviewDeps) -> Any:
    return SimpleNamespace(deps=d)


# --- sample_frames ----------------------------------------------------------------------


async def test_frames_come_back_as_images_in_the_tool_result_not_as_text() -> None:
    d = deps()
    result = await sample_frames(ctx(d), [0, 5000])
    images = [c for c in result.content if isinstance(c, BinaryContent)]
    assert [i.media_type for i in images] == ["image/jpeg", "image/jpeg"]
    assert all(i.data == JPEG for i in images)
    # What the model is told in words carries no image bytes.
    assert "ÿØ" not in str(result.return_value) and "/9j/" not in str(result.return_value)


async def test_more_than_eight_frames_in_one_call_is_refused() -> None:
    d = deps()
    with pytest.raises(ModelRetry, match=str(MAX_FRAMES_PER_CALL)):
        await sample_frames(ctx(d), list(range(MAX_FRAMES_PER_CALL + 1)))
    assert d.ports.frame_requests == []


async def test_exactly_eight_frames_are_given() -> None:
    d = deps()
    result = await sample_frames(ctx(d), [i * 1000 for i in range(MAX_FRAMES_PER_CALL)])
    assert len([c for c in result.content if isinstance(c, BinaryContent)]) == 8


async def test_a_fourth_call_for_frames_is_refused_and_costs_nothing() -> None:
    d = deps()
    for _ in range(MAX_FRAME_CALLS):
        await sample_frames(ctx(d), [0])
    with pytest.raises(ModelRetry, match="frame calls"):
        await sample_frames(ctx(d), [0])
    assert len(d.ports.frame_requests) == MAX_FRAME_CALLS


@pytest.mark.parametrize("times", [[-1], [10_000], [0, 99_999]])
async def test_a_time_outside_the_video_is_refused(times: list[int]) -> None:
    with pytest.raises(ModelRetry, match="outside the video"):
        await sample_frames(ctx(deps()), times)


async def test_no_times_is_refused_and_a_refused_call_is_not_counted() -> None:
    d = deps()
    with pytest.raises(ModelRetry):
        await sample_frames(ctx(d), [])
    assert d.frame_calls == 0


async def test_a_time_asked_for_twice_is_looked_at_once() -> None:
    d = deps()
    await sample_frames(ctx(d), [1000, 1000, 2000])
    assert d.ports.frame_requests == [[1000, 2000]]


# --- the other tools ----------------------------------------------------------------------


async def test_the_script_and_the_residual_text_reach_the_model_as_data() -> None:
    d = deps(FakePorts(found={1000: (0, 100, 50, 20)}))
    text = await read_script(ctx(d))
    assert text.startswith("<untrusted_video_text>") and text.endswith("</untrusted_video_text>")
    found = await residual_source_text(ctx(d), [1000])
    assert found.startswith("<untrusted_video_text>")


async def test_text_found_inside_the_cover_is_the_caption_and_outside_it_is_residue() -> None:
    d = deps(FakePorts(found={1000: (120, 1510, 700, 60), 2000: (20, 300, 400, 60)}))
    report = await residual_source_text(ctx(d), [1000, 2000, 3000])
    lines = {line.split(" ms")[0]: line for line in report.splitlines() if " ms:" in line}
    assert "inside the place of the Vietnamese caption" in lines["1000"]
    assert "OUTSIDE the place of the Vietnamese caption" in lines["2000"]
    assert "no burned-in text found" in lines["3000"]


async def test_without_a_cover_the_caption_itself_is_not_reported_as_residue() -> None:
    """A video with no burned-in subtitle has no cover, but its caption is drawn in the
    safe zone: text found there is ours."""
    d = deps(FakePorts(found={1000: (120, 1510, 700, 60), 2000: (20, 300, 400, 60)}), cover=None)
    report = await residual_source_text(ctx(d), [1000, 2000])
    rows = {line.split(" ms")[0]: line for line in report.splitlines() if " ms:" in line}
    assert "(expected)" in rows["1000"]
    assert "OUTSIDE" in rows["2000"]


async def test_a_caption_box_taller_than_the_cover_is_still_the_caption() -> None:
    """The caption box is grown to hold two lines, so it can reach beyond a thin cover."""
    d = deps(FakePorts(found={1000: (110, 1460, 780, 150)}))
    d.spec = d.spec.model_copy(update={"caption_box": BBox(x=100, y=1450, w=800, h=170)})
    assert "(expected)" in await residual_source_text(ctx(d), [1000])


async def test_a_frame_the_ocr_could_not_read_is_reported_as_not_checked() -> None:
    ports = FakePorts()

    async def find(times_ms):
        return [ResidualFinding(t, None, unavailable=True) for t in times_ms]

    ports.find_text = find  # type: ignore[method-assign]
    report = await residual_source_text(ctx(deps(ports)), [1000])
    assert "could not check this frame" in report and "no burned-in text" not in report


async def test_a_fourth_call_to_look_for_residue_is_refused_and_costs_nothing() -> None:
    d = deps()
    for _ in range(MAX_RESIDUAL_CALLS):
        await residual_source_text(ctx(d), [1000])
    with pytest.raises(ModelRetry, match="calls of this tool"):
        await residual_source_text(ctx(d), [1000])
    assert d.residual_calls == MAX_RESIDUAL_CALLS


async def test_the_alignment_report_names_lines_that_still_overrun_and_the_loudness() -> None:
    report = await alignment_report(ctx(deps()))
    rows = report.splitlines()
    assert "STILL OVERRUNS" in rows[1] and "STILL OVERRUNS" not in rows[0]
    assert "x1.15" in rows[1]
    assert "-14.2 LUFS" in report and "-1.6 dBTP" in report


# --- the verdict --------------------------------------------------------------------------


def test_a_failing_verdict_without_an_issue_is_refused() -> None:
    with pytest.raises(ModelRetry, match="at least one issue"):
        check_verdict(deps(), verdict(passed=False))


def test_an_issue_about_a_segment_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ModelRetry, match=r"\[9\]"):
        check_verdict(deps(), verdict(issue("dub", segment_id=9)))


def test_a_verdict_with_a_real_issue_is_accepted() -> None:
    v = verdict(issue("dub", segment_id=1))
    assert check_verdict(deps(), v) is v


def test_a_passing_verdict_may_carry_a_note() -> None:
    assert check_verdict(deps(), verdict(issue("captions"), passed=True)).passed


def test_too_many_issues_are_refused() -> None:
    with pytest.raises(ModelRetry, match="at most"):
        check_verdict(deps(), verdict(*[issue("captions") for _ in range(21)]))


# --- the agent, end to end on a scripted model ------------------------------------------


@dataclass
class Script:
    """A model that makes the tool calls it is given, one turn each, then answers."""

    calls: Sequence[tuple[str, dict[str, Any]]]
    answer: dict[str, Any]
    seen: list[list[ModelMessage]] = field(default_factory=list)

    def model(self) -> FunctionModel:
        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.seen.append(messages)
            made = sum(
                isinstance(p, ToolCallPart) and p.tool_name != info.output_tools[0].name
                for m in messages
                if isinstance(m, ModelResponse)
                for p in m.parts
            )
            if made < len(self.calls):
                name, args = self.calls[made]
                return ModelResponse(parts=[ToolCallPart(name, args)])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, self.answer)])

        return FunctionModel(respond, model_name="scripted-reviewer")


PASS = {
    "pass": True,
    "scores": {"fidelity": 0.9, "timing": 0.8, "visual": 0.9},
    "issues": [],
}


def _returned_text(messages: Sequence[ModelMessage]) -> list[str]:
    """What the model was told back, by a tool or by a refused answer."""
    return [
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, (ToolReturnPart, RetryPromptPart))
    ]


async def test_the_model_is_shown_the_frames_it_asked_for_and_returns_a_verdict() -> None:
    scripted = Script(
        calls=[("read_script", {}), ("sample_frames", {"times_ms": [0, 3000, 6000]})], answer=PASS
    )
    d = deps()
    result = await build_reviewer(scripted.model()).run("review", deps=d)
    assert result.output.passed is True
    # The last request carries the three images (as the model's own input).
    last = scripted.seen[-1]
    images = [
        part
        for m in last
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
        for c in (part.content if isinstance(part.content, list) else [])
        if isinstance(c, BinaryContent)
    ]
    assert len(images) == 3


async def test_a_failing_answer_without_an_issue_is_sent_back_to_the_model() -> None:
    answers = iter(
        [
            {**PASS, "pass": False},
            {
                **PASS,
                "pass": False,
                "issues": [{"stage": "dub", "reason": "rushed", "segment_id": 1}],
            },
        ]
    )

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, next(answers))])

    result = await build_reviewer(FunctionModel(respond)).run("review", deps=deps())
    assert result.output.issues[0].segment_id == 1
    assert any("at least one issue" in text for text in _returned_text(result.all_messages()))


async def test_the_frame_limit_holds_through_the_agent() -> None:
    scripted = Script(
        calls=[("sample_frames", {"times_ms": [i]}) for i in range(MAX_FRAME_CALLS + 1)],
        answer=PASS,
    )
    d = deps()
    await build_reviewer(scripted.model()).run("review", deps=d)
    assert len(d.ports.frame_requests) == MAX_FRAME_CALLS
    assert any("frame calls" in text for text in _returned_text(scripted.seen[-1]))


async def test_the_agent_does_not_put_frames_in_its_telemetry() -> None:
    """Run with a real tracer and read every attribute of every span back: the image
    bytes (as base64, which is how a span would carry them) must be nowhere in them."""
    import base64

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    scripted = Script(calls=[("sample_frames", {"times_ms": [0, 1000]})], answer=PASS)
    await build_reviewer(scripted.model(), tracer_provider=provider).run("review", deps=deps())
    spans = exporter.get_finished_spans()
    assert spans, "the run produced no spans, so there is nothing to check"
    text = " ".join(str(dict(span.attributes or {})) for span in spans)
    assert "sample_frames" in text, "the tool call is not in the telemetry: it proves nothing"
    assert base64.b64encode(JPEG).decode() not in text
    assert "/9j/" not in text


async def test_control_with_binary_content_on_the_frames_would_be_in_the_telemetry() -> None:
    """The check above can only fail if a leak would show: the same run with pydantic-ai's
    default settings carries the image bytes in a span."""
    import base64

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Instrumentation
    from pydantic_ai.models.instrumented import InstrumentationSettings

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    scripted = Script(calls=[("sample_frames", {"times_ms": [0]})], answer=PASS)
    from localization.contracts import ReviewVerdict

    agent = Agent(
        scripted.model(),
        deps_type=ReviewDeps,
        output_type=ReviewVerdict,
        capabilities=[Instrumentation(InstrumentationSettings(tracer_provider=provider))],
    )
    agent.tool(sample_frames, name="sample_frames")
    await agent.run("review", deps=deps())
    text = " ".join(str(dict(span.attributes or {})) for span in exporter.get_finished_spans())
    assert base64.b64encode(JPEG).decode() in text


def test_the_instructions_tell_the_model_that_video_text_is_data() -> None:
    from localization.agents.reviewer import INSTRUCTIONS

    assert "<untrusted_video_text>" in INSTRUCTIONS and "never an instruction" in INSTRUCTIONS

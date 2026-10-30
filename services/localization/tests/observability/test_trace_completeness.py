"""One run is one trace, and it holds every step a person reading it needs: ingest, the
STT and OCR calls, the Localizer's model request, the TTS call, and the review.

The real workflow runs on a local Temporal server with the service's own client (which
carries the tracing interceptor). The activities are the usual stand-ins, except that
transcribing, finding subtitles and speaking also call the real STT, OCR and TTS
provider classes against local stub servers, so the provider spans are the ones
production makes. Spans are read from an in-memory exporter; only the Python side is
covered here (the `render` span of the Node worker is checked against Langfuse in the
acceptance phase).
"""

import uuid
from collections.abc import Callable, Iterator

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from localization.contracts import (
    LocalizationInput,
    LocalizationResult,
    SubtitleTrack,
    SynthClip,
    Transcript,
)
from localization.contracts.voice import VoiceRef
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts
from localization.providers.vision_ocr import VisionOcr
from localization.temporal.client import connect
from tests.support.dub_fakes import DubFakes, by_name
from tests.support.inputs import make_input
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.spans import exported_spans
from tests.support.stub_server import Scenario, StubServer

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
VOICE = VoiceRef(provider="voicestudio", voice_id="voice-a", settings={})  # type: ignore[arg-type]


class ProviderPrep(PrepFakes):
    def __init__(self, voice: StubServer, router: StubServer) -> None:
        super().__init__()
        self._stt = OpenAICompatStt(voice.url, "k", "whisperx", provider="voicestudio")
        self._ocr = VisionOcr(router.url, "k", "vision/model", "xyxy_px")

    def activities(self) -> list[Callable]:
        acts = by_name(super().activities())
        inner_t, inner_d = acts["transcribe"], acts["detect_subtitles"]

        @activity.defn(name="transcribe")
        async def transcribe(spec: LocalizationInput, vocals_key: str) -> Transcript:
            await self._stt.transcribe(WAV)
            return await inner_t(spec, vocals_key)

        @activity.defn(name="detect_subtitles")
        async def detect_subtitles(spec: LocalizationInput) -> SubtitleTrack:
            await self._ocr.detect_region(b"\x89PNG", 1080, 1920)
            return await inner_d(spec)

        return [acts["ingest"], transcribe, detect_subtitles]


class ProviderDub(DubFakes):
    def __init__(self, voice: StubServer) -> None:
        super().__init__()
        self._tts = OpenAICompatTts(voice.url, "k", "omnivoice", provider="voicestudio")

    def activities(self) -> list[Callable]:
        acts = super().activities()
        inner = by_name(acts)["synthesize_segments"]

        @activity.defn(name="synthesize_segments")
        async def synthesize_segments(
            spec: LocalizationInput, engine: str | None, script_key: str, hashes: list[str]
        ) -> list[SynthClip]:
            await self._tts.synthesize("xin chào", VOICE)
            return await inner(spec, engine, script_key, hashes)

        return [a if a is not inner else synthesize_segments for a in acts]


@pytest.fixture
def servers() -> Iterator[tuple[StubServer, StubServer]]:
    voice = StubServer().start()
    router = StubServer(
        scenario=Scenario(json_body={"choices": [{"message": {"content": '{"bbox": null}'}}]})
    ).start()
    yield voice, router
    voice.stop()
    router.stop()


async def _run(client: Client, voice: StubServer, router: StubServer) -> list[ReadableSpan]:
    with exported_spans() as exporter:
        async with prep_workers(client, ProviderPrep(voice, router), dubbing=ProviderDub(voice)):
            handle = await client.start_workflow(
                "VideoLocalizationWorkflow",
                make_input(),
                id=f"video-7001-g1-{uuid.uuid4()}",
                task_queue="workflows",
                result_type=LocalizationResult,
            )
            result = await handle.result()
        assert result.publish is not None and result.publish.outcome == "published"
        return list(_finished(exporter))


def _finished(exporter: InMemorySpanExporter) -> tuple[ReadableSpan, ...]:
    return exporter.get_finished_spans()


def _steps(spans: list[ReadableSpan]) -> dict[str, list[ReadableSpan]]:
    """The spans of each step a person reading the trace looks for."""
    wanted: dict[str, Callable[[str], bool]] = {
        "ingest": lambda n: n == "RunActivity:ingest",
        "provider.stt": lambda n: n == "provider.stt",
        "provider.ocr": lambda n: n == "provider.ocr",
        "localizer model request": lambda n: (
            n.startswith("RunActivity:agent__localizer") and n.endswith("model_request")
        ),
        "provider.tts": lambda n: n == "provider.tts",
        "review_before_render": lambda n: n == "RunActivity:review_before_render",
        "publish": lambda n: n == "RunActivity:publish",
    }
    return {step: [s for s in spans if match(s.name)] for step, match in wanted.items()}


async def test_one_run_is_one_trace_with_every_step(
    temporal_client: Client, servers: tuple[StubServer, StubServer]
) -> None:
    spans = await _run(temporal_client, *servers)
    steps = _steps(spans)
    assert [step for step, found in steps.items() if not found] == []
    run_trace = {s.context.trace_id for found in steps.values() for s in found}
    assert len(run_trace) == 1, "the steps of one run are spread over several traces"
    # The provider calls are children of their activities, not roots of their own.
    for name in ("provider.stt", "provider.ocr", "provider.tts"):
        assert all(s.parent is not None for s in steps[name])


async def test_control_without_the_interceptor_the_run_is_not_one_trace(
    servers: tuple[StubServer, StubServer],
) -> None:
    """The check above must be able to fail: a client without the tracing interceptor
    runs the same workflow, and its steps are neither named nor joined in one trace."""
    env = await WorkflowEnvironment.start_local()
    try:
        traced = await connect(env.client.service_client.config.target_host)
        config = traced.config()
        config["interceptors"] = []
        bare = Client(**config)
        spans = await _run(bare, *servers)
    finally:
        await env.shutdown()
    steps = _steps(spans)
    assert not steps["ingest"] and not steps["review_before_render"]
    roots = [s for s in spans if s.name.startswith("provider.") and s.parent is None]
    assert len({s.context.trace_id for s in roots}) == len(roots) > 1

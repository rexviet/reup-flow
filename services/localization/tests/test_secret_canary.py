"""No secret in workflow input, history, spans, stdout or stderr.

Every credential in the environment is a `canary-<uuid>`. The real provider class
runs against a local HTTP server (200, 401, 500, timeout), and the server's error
bodies echo the Authorization header the way a careless gateway does. Then
history (including failure payloads), exported spans and the captured output
streams are searched for any canary.
"""

import sys
import uuid
from dataclasses import dataclass

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Worker

from localization.config import Settings, voice_ref_from_settings
from localization.contracts import LocalizationInput
from tests.support.canary_flow import TASK_QUEUE, SynthesizeWorkflow, make_synthesize_activity
from tests.support.stub_server import Scenario, StubServer

_EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="session", autouse=True)
def _span_exporter() -> InMemorySpanExporter:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(provider)
    return _EXPORTER


@dataclass
class Canaries:
    voicestudio: str
    openrouter: str
    s3_id: str
    s3_secret: str

    def all(self) -> list[str]:
        return [self.voicestudio, self.openrouter, self.s3_id, self.s3_secret]


@pytest.fixture
def canaries(monkeypatch: pytest.MonkeyPatch, stub: StubServer) -> Canaries:
    c = Canaries(*(f"canary-{uuid.uuid4()}" for _ in range(4)))
    monkeypatch.setenv("VOICESTUDIO_URL", stub.url)
    monkeypatch.setenv("VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("VOICESTUDIO_API_KEY", c.voicestudio)
    monkeypatch.setenv("OPENROUTER_API_KEY", c.openrouter)
    monkeypatch.setenv("LOC_S3_ACCESS_KEY_ID", c.s3_id)
    monkeypatch.setenv("LOC_S3_SECRET_ACCESS_KEY", c.s3_secret)
    monkeypatch.setenv("VOICE_ID", "voice-1")
    monkeypatch.setenv("TTS_MODEL", "tts-model")
    monkeypatch.setenv("PROVIDER_TIMEOUT_SECONDS", "0.5")
    return c


def find_leaks(
    canaries: list[str], *, history: bytes, spans: str, output: str
) -> dict[str, list[str]]:
    """Which channel carries which canary. Empty means clean."""
    leaks: dict[str, list[str]] = {}
    for channel, haystack in (
        ("history", history),
        ("spans", spans.encode()),
        ("output", output.encode()),
    ):
        found = [c for c in canaries if c.encode() in haystack]
        if found:
            leaks[channel] = found
    return leaks


def _span_text() -> str:
    parts: list[str] = []
    for span in _EXPORTER.get_finished_spans():
        parts.append(span.name)
        parts.append(repr(dict(span.attributes or {})))
        parts.append(repr(span.status.description))
        for event in span.events:
            parts.append(event.name)
            parts.append(repr(dict(event.attributes or {})))
    return "\n".join(parts)


async def _run_scenario(
    client: Client, settings: Settings
) -> tuple[bytes, WorkflowFailureError | None]:
    spec = LocalizationInput(
        aweme_id="7312345678901234567",
        channel_id="chan",
        generation=0,
        video_key="source/7312345678901234567.mp4",
        target_lang="vi",
        voice=voice_ref_from_settings(settings),
    )
    failure: WorkflowFailureError | None = None
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SynthesizeWorkflow],
        activities=[make_synthesize_activity(settings)],
    ):
        handle = await client.start_workflow(
            SynthesizeWorkflow.run,
            spec,
            id=f"canary-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        try:
            await handle.result()
        except WorkflowFailureError as exc:
            failure = exc
        history = await handle.fetch_history()
    # Protobuf bytes keep payload data raw, unlike to_json() which base64-encodes it.
    return b"".join(event.SerializeToString() for event in history.events), failure


SCENARIOS = {
    "200": Scenario(status=200),
    "401": Scenario(status=401, echo_authorization=True),
    "500": Scenario(status=500, echo_authorization=True),
    "timeout": Scenario(delay_seconds=1.5),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_no_canary_reaches_history_spans_or_output(
    name: str,
    canaries: Canaries,
    stub: StubServer,
    temporal_client: Client,
    capfd: pytest.CaptureFixture[str],
) -> None:
    _EXPORTER.clear()
    stub.scenario = SCENARIOS[name]
    settings = Settings()

    history, failure = await _run_scenario(temporal_client, settings)
    captured = capfd.readouterr()

    # The scenario really happened, and the key really travelled to the provider.
    assert stub.requests, "the provider never called the server"
    assert stub.requests[0].headers["authorization"] == f"Bearer {canaries.voicestudio}"
    if name == "200":
        assert failure is None
    else:
        assert failure is not None
    assert any(s.name == "provider.tts" for s in _EXPORTER.get_finished_spans())

    leaks = find_leaks(
        canaries.all(),
        history=history,
        spans=_span_text(),
        output=captured.out + captured.err,
    )
    assert leaks == {}


# --- controls: the scanner must see a leak in every channel -----------------


def test_control_scanner_sees_a_canary_in_every_channel(
    canaries: Canaries, capfd: pytest.CaptureFixture[str]
) -> None:
    _EXPORTER.clear()
    tracer = trace.get_tracer("control")
    with tracer.start_as_current_span("leaky", attributes={"auth": canaries.openrouter}):
        pass
    print(canaries.s3_secret, file=sys.stderr)
    captured = capfd.readouterr()
    leaks = find_leaks(
        canaries.all(),
        history=f"...{canaries.voicestudio}...".encode(),
        spans=_span_text(),
        output=captured.out + captured.err,
    )
    assert leaks == {
        "history": [canaries.voicestudio],
        "spans": [canaries.openrouter],
        "output": [canaries.s3_secret],
    }


async def test_control_a_leak_through_an_activity_failure_is_caught(
    canaries: Canaries, stub: StubServer, temporal_client: Client
) -> None:
    from temporalio import activity
    from temporalio.exceptions import ApplicationError

    @activity.defn(name="synthesize")
    async def leaky(spec: LocalizationInput) -> int:
        raise ApplicationError(f"upstream said: Bearer {canaries.voicestudio}", non_retryable=True)

    settings = Settings()
    spec = LocalizationInput(
        aweme_id="1",
        channel_id="c",
        generation=0,
        video_key="k",
        target_lang="vi",
        voice=voice_ref_from_settings(settings),
    )
    async with Worker(
        temporal_client,
        task_queue=TASK_QUEUE,
        workflows=[SynthesizeWorkflow],
        activities=[leaky],
    ):
        handle = await temporal_client.start_workflow(
            SynthesizeWorkflow.run, spec, id=f"canary-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        history = await handle.fetch_history()
    raw = b"".join(e.SerializeToString() for e in history.events)
    assert find_leaks(canaries.all(), history=raw, spans="", output="") == {
        "history": [canaries.voicestudio]
    }


def test_credential_settings_are_not_part_of_the_workflow_input(canaries: Canaries) -> None:
    spec = voice_ref_from_settings(Settings())
    dumped = spec.model_dump_json()
    assert not [c for c in canaries.all() if c in dumped]

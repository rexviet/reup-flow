"""No secret in workflow input, history, spans, stdout or stderr.

Every credential in the environment is a `canary-<uuid>`. The real provider
classes (TTS and STT with the VoiceStudio key, OCR with the OpenRouter key, the
object store with the S3 keys) run against a local HTTP server or an in-process
S3, in success and failure scenarios, and the server's error bodies echo the
Authorization header the way a careless gateway does. Then history (including
failure payloads), exported spans and the captured output streams are searched
for any canary.
"""

import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import boto3
import pytest
from moto import mock_aws
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

from localization.config import Settings, voice_ref_from_settings
from localization.contracts import LocalizationInput
from localization.storage.object_store import ObjectStore
from tests.support.canary_flow import (
    ARTIFACT_KEY,
    TASK_QUEUE,
    ProviderWorkflow,
    make_activities,
)
from tests.support.stub_server import Scenario, StubServer

_EXPORTER = InMemorySpanExporter()
BUCKET = "reup-media"


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
    monkeypatch.setenv("OPENROUTER_BASE_URL", stub.url)
    monkeypatch.setenv("OPENROUTER_API_KEY", c.openrouter)
    monkeypatch.setenv("LOC_S3_ACCESS_KEY_ID", c.s3_id)
    monkeypatch.setenv("LOC_S3_SECRET_ACCESS_KEY", c.s3_secret)
    monkeypatch.setenv("VOICE_ID", "voice-1")
    monkeypatch.setenv("TTS_MODEL", "tts-model")
    monkeypatch.setenv("STT_MODEL", "stt-model")
    monkeypatch.setenv("OCR_MODEL", "vision/model")
    monkeypatch.setenv("OCR_BBOX_FORMAT", "xyxy_px")
    monkeypatch.setenv("PROVIDER_TIMEOUT_SECONDS", "0.5")
    return c


@pytest.fixture
def s3_store(canaries: Canaries) -> Iterator[ObjectStore]:
    """An in-process S3 reached through the real `ObjectStore.from_settings`, so
    the S3 canaries are the credentials the boto client really signs with."""
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        store = ObjectStore.from_settings(Settings())
        yield store


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


def _spec(settings: Settings) -> LocalizationInput:
    return LocalizationInput(
        aweme_id="7312345678901234567",
        channel_id="chan",
        generation=0,
        video_key="source/7312345678901234567.mp4",
        target_lang="vi",
        voice=voice_ref_from_settings(settings),
    )


async def _run(
    client: Client, kind: str, settings: Settings, store: ObjectStore | None
) -> tuple[bytes, WorkflowFailureError | None]:
    failure: WorkflowFailureError | None = None
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[ProviderWorkflow],
        activities=make_activities(settings, store),
    ):
        handle = await client.start_workflow(
            ProviderWorkflow.run,
            args=[kind, _spec(settings)],
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


def _chat(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


OK = {
    "synthesize": Scenario(status=200),
    "transcribe": Scenario(status=200),
    "detect_region": Scenario(json_body=_chat('{"bbox": [100, 800, 900, 900]}')),
}


def _http_scenarios(kind: str) -> dict[str, Scenario]:
    return {
        "200": OK[kind],
        "401": Scenario(status=401, echo_authorization=True),
        "500": Scenario(status=500, echo_authorization=True),
        "timeout": Scenario(delay_seconds=1.5),
    }


# kind -> (span name, whose key the provider must have sent)
HTTP_KINDS = {
    "synthesize": ("provider.tts", "voicestudio"),
    "transcribe": ("provider.stt", "voicestudio"),
    "detect_region": ("provider.ocr", "openrouter"),
}
HTTP_CASES = [(kind, name) for kind in HTTP_KINDS for name in ("200", "401", "500", "timeout")]


@pytest.mark.parametrize("kind, name", HTTP_CASES)
async def test_no_canary_reaches_history_spans_or_output(
    kind: str,
    name: str,
    canaries: Canaries,
    stub: StubServer,
    temporal_client: Client,
    capfd: pytest.CaptureFixture[str],
) -> None:
    _EXPORTER.clear()
    stub.scenario = _http_scenarios(kind)[name]
    settings = Settings()

    history, failure = await _run(temporal_client, kind, settings, None)
    captured = capfd.readouterr()

    # The scenario really happened, and the right key really travelled.
    span_name, owner = HTTP_KINDS[kind]
    sent = getattr(canaries, owner)
    assert stub.requests, "the provider never called the server"
    assert stub.requests[0].headers["authorization"] == f"Bearer {sent}"
    assert (failure is None) == (name == "200")
    assert any(s.name == span_name for s in _EXPORTER.get_finished_spans())

    leaks = find_leaks(
        canaries.all(), history=history, spans=_span_text(), output=captured.out + captured.err
    )
    assert leaks == {}


@pytest.mark.parametrize("present", [True, False], ids=["present", "missing"])
async def test_no_s3_canary_reaches_history_spans_or_output(
    present: bool,
    canaries: Canaries,
    s3_store: ObjectStore,
    temporal_client: Client,
    capfd: pytest.CaptureFixture[str],
) -> None:
    _EXPORTER.clear()
    if present:
        s3_store.put_bytes(ARTIFACT_KEY, b"data")
    settings = Settings()
    # The store really signs with the canary credentials.
    signer_credentials = s3_store._client._request_signer._credentials  # noqa: SLF001
    assert signer_credentials.access_key == canaries.s3_id
    assert signer_credentials.secret_key == canaries.s3_secret

    history, failure = await _run(temporal_client, "fetch_artifact", settings, s3_store)
    captured = capfd.readouterr()

    if present:
        assert failure is None
    else:
        assert failure is not None
        activity_error = failure.cause
        assert isinstance(activity_error, ActivityError)
        assert isinstance(activity_error.cause, ApplicationError)
        assert activity_error.cause.type == "ArtifactMissingError"
    leaks = find_leaks(
        canaries.all(), history=history, spans=_span_text(), output=captured.out + captured.err
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
    canaries: Canaries, temporal_client: Client
) -> None:
    from temporalio import activity

    @activity.defn(name="synthesize")
    async def leaky(spec: LocalizationInput) -> int:
        raise ApplicationError(f"upstream said: Bearer {canaries.voicestudio}", non_retryable=True)

    settings = Settings()
    async with Worker(
        temporal_client,
        task_queue=TASK_QUEUE,
        workflows=[ProviderWorkflow],
        activities=[leaky],
    ):
        handle = await temporal_client.start_workflow(
            ProviderWorkflow.run,
            args=["synthesize", _spec(settings)],
            id=f"canary-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
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

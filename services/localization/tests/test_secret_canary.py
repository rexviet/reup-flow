"""No secret in workflow input, history, spans, stdout or stderr.

Every credential in the environment is a `canary-<uuid>`. The real provider
classes (TTS and STT with the VoiceStudio key, OCR with the OpenRouter key, the
object store with the S3 keys, Vbee STT and TTS with the Vbee token, app id and
voice id) run against a local HTTP server or an in-process
S3, in success and failure scenarios, and the server's error bodies echo the
Authorization header the way a careless gateway does. Then history (including
failure payloads), exported spans and the captured output streams are searched
for any canary.
"""

import io
import logging
import sys
import uuid
import wave
from collections.abc import Iterator
from dataclasses import dataclass

import boto3
import httpx
import pytest
from moto import mock_aws
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

from localization.config import Settings, voice_ref_from_settings
from localization.contracts import LocalizationInput
from localization.contracts.voice import VbeeSettings, VoiceRef
from localization.providers.errors import ProviderError, to_application_error
from localization.providers.vbee import VbeeStt, VbeeTts
from localization.storage.object_store import ObjectStore
from tests.support.canary_flow import (
    ARTIFACT_KEY,
    TASK_QUEUE,
    ProviderWorkflow,
    make_activities,
)
from tests.support.spans import global_exporter
from tests.support.stub_server import Scenario, StubServer

_EXPORTER: InMemorySpanExporter = global_exporter()
BUCKET = "reup-media"


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


# --- Vbee: token, app id and voice id ----------------------------------------------


@dataclass
class VbeeCanaries:
    token: str
    app_id: str
    voice_id: str

    def all(self) -> list[str]:
        return [self.token, self.app_id, self.voice_id]


@pytest.fixture
def vbee_canaries(monkeypatch: pytest.MonkeyPatch, canaries: Canaries) -> VbeeCanaries:
    c = VbeeCanaries(*(f"canary-{uuid.uuid4()}" for _ in range(3)))
    monkeypatch.setenv("VBEE_BASE_URL", "https://api.vbee.canary.test")
    monkeypatch.setenv("VBEE_API_TOKEN", c.token)
    monkeypatch.setenv("VBEE_APP_ID", c.app_id)
    monkeypatch.setenv("VBEE_VOICE_ID", c.voice_id)
    return c


def _wav_1s() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00" * 16000)
    return buffer.getvalue()


class FakeVbee:
    """Vbee's routes, and a failure in one place per scenario. Every error body
    echoes what a careless gateway would: the Authorization and App-Id headers and the
    request body, which holds the voice id."""

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.requests: list[httpx.Request] = []
        self.tts_body = ""

    def _echo(self, request: httpx.Request) -> str:
        return " ".join(
            [
                request.headers.get("authorization", ""),
                request.headers.get("app-id", ""),
                request.content.decode(errors="replace")[:2000],
            ]
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, sc = request.url.path, self.scenario
        submit = request.method == "POST"
        if submit and sc == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if submit and sc in ("401", "403", "500"):
            code = {"401": "UNAUTHORIZED", "403": "FORBIDDEN", "500": "INTERNAL_SERVER_ERROR"}[sc]
            body = {"error": {"code": code, "message": self._echo(request)}}
            return httpx.Response(int(sc), json=body)
        if path == "/v1/stt":
            return httpx.Response(200, json={"transcriptId": "t1", "status": "PENDING"})
        if path == "/v1/tts":
            self.tts_body = request.content.decode()
            return httpx.Response(200, json={"requestId": "r1", "status": "PROCESSING"})
        if sc == "job_failed" and path.startswith(("/v1/stt/", "/v1/tts/")):
            message = f"failed: {self.tts_body} {self._echo(request)}"
            error = {"code": "BAD_REQUEST", "message": message}
            return httpx.Response(200, json={"status": "FAILED", "error": error})
        if path == "/v1/stt/transcripts/t1":
            utterance = {"text": "xin chào", "startTime": 0, "endTime": 0.9}
            return httpx.Response(200, json={"status": "COMPLETED", "utterances": [utterance]})
        if path == "/v1/tts/requests/r1":
            link = "https://cdn.vbee.canary.test/a.wav"
            return httpx.Response(200, json={"status": "COMPLETED", "audioLink": link})
        if request.url.host == "cdn.vbee.canary.test":
            if sc == "expired":
                return httpx.Response(403, text=f"expired {self._echo(request)}")
            return httpx.Response(200, content=_wav_1s())
        return httpx.Response(404)


def make_vbee_activities(settings: Settings, fake: FakeVbee):
    """The real Vbee providers, built from the settings, over the fake. The voice is
    read from the settings inside the activity, as a provider-side secret."""

    def http() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake))

    @activity.defn(name="vbee_synthesize")
    async def vbee_synthesize(spec: LocalizationInput) -> int:
        assert settings.vbee_voice_id is not None
        voice = VoiceRef(
            provider="vbee",
            voice_id=settings.vbee_voice_id.get_secret_value(),
            settings=VbeeSettings(),
        )
        tts = VbeeTts.from_settings(settings, http(), poll_interval_seconds=0.0)
        try:
            return len(await tts.synthesize("xin chào", voice))
        except ProviderError as exc:
            logging.getLogger("canary").warning("vbee tts failed: %s", exc)
            raise to_application_error(exc) from None

    @activity.defn(name="vbee_transcribe")
    async def vbee_transcribe(spec: LocalizationInput) -> int:
        stt = VbeeStt.from_settings(settings, http(), poll_interval_seconds=0.0)
        try:
            return len((await stt.transcribe(_wav_1s())).utterances)
        except ProviderError as exc:
            logging.getLogger("canary").warning("vbee stt failed: %s", exc)
            raise to_application_error(exc) from None

    return [vbee_synthesize, vbee_transcribe]


VBEE_KINDS = {"vbee_synthesize": "provider.tts", "vbee_transcribe": "provider.stt"}
VBEE_SCENARIOS = ("200", "401", "403", "500", "timeout", "job_failed")
VBEE_CASES = [(kind, sc) for kind in VBEE_KINDS for sc in VBEE_SCENARIOS] + [
    ("vbee_synthesize", "expired")
]


@pytest.mark.parametrize("kind, scenario", VBEE_CASES)
async def test_no_vbee_canary_reaches_history_spans_logs_or_output(
    kind: str,
    scenario: str,
    vbee_canaries: VbeeCanaries,
    temporal_client: Client,
    capfd: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _EXPORTER.clear()
    caplog.set_level(logging.DEBUG)
    settings = Settings()
    fake = FakeVbee(scenario)

    async with Worker(
        temporal_client,
        task_queue=TASK_QUEUE,
        workflows=[ProviderWorkflow],
        activities=make_vbee_activities(settings, fake),
    ):
        handle = await temporal_client.start_workflow(
            ProviderWorkflow.run,
            args=[kind, _spec(settings)],
            id=f"canary-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        failure: WorkflowFailureError | None = None
        try:
            await handle.result()
        except WorkflowFailureError as exc:
            failure = exc
        history = b"".join(e.SerializeToString() for e in (await handle.fetch_history()).events)
    captured = capfd.readouterr()

    # The scenario really happened, and the real credentials really travelled.
    assert fake.requests, "the provider never called the fake"
    first = fake.requests[0]
    assert first.headers["authorization"] == f"Bearer {vbee_canaries.token}"
    assert first.headers["app-id"] == vbee_canaries.app_id
    if kind == "vbee_synthesize":
        assert vbee_canaries.voice_id in first.content.decode()
    assert (failure is None) == (scenario == "200")
    assert any(s.name == VBEE_KINDS[kind] for s in _EXPORTER.get_finished_spans())

    leaks = find_leaks(
        vbee_canaries.all(),
        history=history,
        spans=_span_text(),
        output=captured.out + captured.err + caplog.text,
    )
    assert leaks == {}


def test_control_the_vbee_scanner_sees_each_vbee_canary(
    vbee_canaries: VbeeCanaries,
) -> None:
    c = vbee_canaries
    leaks = find_leaks(
        c.all(),
        history=f"Bearer {c.token}".encode(),
        spans=f"App-Id {c.app_id}",
        output=f"voiceCode {c.voice_id}",
    )
    assert leaks == {"history": [c.token], "spans": [c.app_id], "output": [c.voice_id]}


def test_vbee_settings_never_show_their_secrets(vbee_canaries: VbeeCanaries) -> None:
    settings = Settings()
    shown = repr(settings) + str(settings) + settings.model_dump_json()
    assert [c for c in vbee_canaries.all() if c in shown] == []

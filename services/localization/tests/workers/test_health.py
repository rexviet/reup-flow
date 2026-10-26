import httpx
import pytest

from localization.workers.health import WELL_KNOWN_PATH, VoiceStudioHealth, active_engine


def _health(handler) -> VoiceStudioHealth:  # noqa: ANN001
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VoiceStudioHealth("https://voice.example", "k-secret", http=http)


@pytest.mark.parametrize(
    ("body", "engine"),
    [
        ({"engine": "omnivoice"}, "omnivoice"),
        ({"active_engine": "kokoro", "engine": "x"}, "kokoro"),
        ({"tts": {"engine": "f5"}}, "f5"),
        ({}, None),
        ([], None),
    ],
)
def test_the_active_engine_is_read_from_the_well_known_document(body, engine) -> None:  # noqa: ANN001
    assert active_engine(body) == engine


async def test_a_healthy_voicestudio_reports_its_engine_and_is_asked_with_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"engine": "omnivoice"})

    state = await _health(handler).check_once()
    assert (state.ok, state.engine) == (True, "omnivoice")
    assert seen[0].url.path == WELL_KNOWN_PATH
    assert seen[0].headers["authorization"] == "Bearer k-secret"


async def test_an_outage_is_a_state_not_an_exception_and_keeps_the_last_engine() -> None:
    answers = iter([httpx.Response(200, json={"engine": "omnivoice"}), httpx.Response(503)])
    health = _health(lambda request: next(answers))
    await health.check_once()
    state = await health.check_once()
    assert (state.ok, state.engine) == (False, "omnivoice")


async def test_an_unreachable_voicestudio_is_a_state_too() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    state = await _health(handler).check_once()
    assert state.ok is False

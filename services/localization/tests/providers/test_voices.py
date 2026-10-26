import pytest
from tests.support.stub_server import Scenario, StubServer

from localization.config import ConfigError, Settings
from localization.providers.voices import VoiceCatalog, VoiceUnavailable, parse_voice_ids


@pytest.mark.parametrize(
    "body",
    [
        {"voices": [{"id": "a"}, {"voice_id": "b"}, {"name": "c"}]},
        {"data": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
        [{"id": "a"}, {"id": "b"}, "c"],
    ],
)
def test_every_known_voice_list_shape_is_understood(body) -> None:  # noqa: ANN001
    assert parse_voice_ids(body) == {"a", "b", "c"}


def _catalog(stub: StubServer, monkeypatch: pytest.MonkeyPatch, **kw) -> VoiceCatalog:  # noqa: ANN003
    monkeypatch.setenv("VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS", "127.0.0.0/8")
    monkeypatch.setenv("VOICESTUDIO_API_KEY", "key-1")
    return VoiceCatalog.from_settings(Settings(voicestudio_url=stub.url), **kw)


async def test_a_listed_voice_passes_and_an_unlisted_one_is_refused(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(stub, monkeypatch)
    await catalog.ensure_exists("voice-a")
    with pytest.raises(ConfigError, match="VOICE_ID"):
        await catalog.ensure_exists("not-there")
    assert stub.requests[0].headers["authorization"] == "Bearer key-1"
    assert stub.requests[0].path == "/v1/audio/voices"


async def test_a_confirmed_voice_is_not_asked_about_again_until_the_ttl(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    catalog = _catalog(stub, monkeypatch, ttl_seconds=60.0, clock=lambda: now[0])
    await catalog.ensure_exists("voice-a")
    await catalog.ensure_exists("voice-a")
    assert len(stub.requests) == 1
    now[0] = 61.0
    await catalog.ensure_exists("voice-a")
    assert len(stub.requests) == 2


async def test_an_unreachable_provider_is_neither_a_pass_nor_a_refusal(
    stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.scenario = Scenario(status=503)
    with pytest.raises(VoiceUnavailable):
        await _catalog(stub, monkeypatch).ensure_exists("voice-a")


def test_validation_needs_the_provider_url() -> None:
    with pytest.raises(ConfigError):
        VoiceCatalog.from_settings(Settings())

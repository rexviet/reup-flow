"""The local voice server drops many new connections, so the providers that talk to it open a
connection within a second or give up on it and try again, and keep connections for reuse."""

import httpx
import pytest

from localization.providers.base import (
    LOCAL_CONNECT_TIMEOUT_SECONDS,
    OpenAICompatClient,
    local_server_http,
)
from localization.providers.openai_stt import OpenAICompatStt
from localization.providers.openai_tts import OpenAICompatTts
from localization.providers.voices import VoiceCatalog


def spy(timeouts: list) -> httpx.AsyncClient:
    def answer(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    return httpx.AsyncClient(transport=httpx.MockTransport(answer))


async def test_connect_has_its_own_short_timeout_and_reading_keeps_the_long_one() -> None:
    seen: list = []
    client = OpenAICompatClient(
        "tts", "http://local.test", None, timeout_seconds=120.0, http=spy(seen), connect_timeout=1.0
    )
    await client.get("/x")
    assert seen == [{"connect": 1.0, "read": 120.0, "write": 120.0, "pool": 120.0}]


async def test_without_a_connect_timeout_one_number_bounds_everything() -> None:
    seen: list = []
    client = OpenAICompatClient("x", "http://r.test", None, timeout_seconds=30.0, http=spy(seen))
    await client.get("/")
    assert seen == [{"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}]


@pytest.mark.parametrize(
    "build",
    [
        lambda: OpenAICompatStt("http://local.test", None, "m"),
        lambda: OpenAICompatTts("http://local.test", None, "m"),
        lambda: VoiceCatalog("http://local.test", None),
    ],
)
def test_the_voice_providers_use_the_short_connect_timeout_and_the_local_client(build) -> None:
    provider = build()
    client = provider._client
    assert client._timeout.connect == LOCAL_CONNECT_TIMEOUT_SECONDS
    assert isinstance(client._http, httpx.AsyncClient)


def test_the_local_client_is_a_fresh_pooled_client_each_time() -> None:
    assert local_server_http() is not local_server_http()

"""A model request that gets no answer fails when its timeout says and is retried.

The server accepts the connection, reads the request and never writes a byte: what a
stalled provider (or a model that reasons past any reasonable time) looks like to the
client. The model is the production one, built by `model_for`, over the shared client.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal._model_errors import model_errors_as_application_errors
from pydantic_ai.exceptions import ModelAPIError
from temporalio.exceptions import ApplicationError

from localization.agents import temporal as agent_temporal
from localization.config import Settings
from localization.providers.llm import REQUEST_TIMEOUT_SECONDS, model_for, openrouter_client

TIMEOUT = 0.6


class Silent:
    """A server that takes connections and never answers."""

    def __init__(self) -> None:
        self.connections = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._held: list[asyncio.StreamWriter] = []

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._held.append(writer)
        await reader.read(65536)
        await asyncio.sleep(60)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for writer in self._held:
            writer.close()
        assert self._server is not None
        self._server.close()


@pytest.fixture
async def silent() -> AsyncIterator[Silent]:
    server = Silent()
    await server.start()
    yield server
    await server.stop()


def settings_for(server: Silent) -> Settings:
    return Settings(
        openrouter_base_url=f"http://127.0.0.1:{server.port}/v1",
        openrouter_api_key="k",  # type: ignore[arg-type]
        localizer_model="some/model",  # type: ignore[arg-type]
        voicestudio_allow_private_http_cidrs="127.0.0.0/8",
    )


async def ask(server: Silent) -> tuple[float, BaseException]:
    # The shared client keeps the library's long default; the model's own timeout is what
    # must cut the request, as it does in the worker.
    http = openrouter_client()
    model = model_for("localizer", settings_for(server), http, timeout=TIMEOUT)
    started = time.monotonic()
    with pytest.raises(ModelAPIError) as info:
        await Agent(model).run("hello")
    await http.aclose()
    return time.monotonic() - started, info.value


async def test_a_request_with_no_answer_fails_when_the_timeout_says(silent: Silent) -> None:
    elapsed, _ = await ask(silent)
    assert TIMEOUT * 0.8 <= elapsed < TIMEOUT * 4


async def test_the_client_does_not_retry_by_itself(silent: Silent) -> None:
    """The OpenAI client retries a timed-out request twice unless told not to: that
    would make one stalled attempt cost three timeouts, inside Temporal's own retries."""
    await ask(silent)
    assert silent.connections == 1


async def test_the_failure_is_one_temporal_retries(silent: Silent) -> None:
    _, error = await ask(silent)
    with pytest.raises(BaseException) as info, model_errors_as_application_errors():
        raise error
    raised = info.value
    # Either left as it is (Temporal retries any error that is not marked otherwise) or
    # wrapped as an error of a type the policy does not list as permanent.
    if isinstance(raised, ApplicationError):
        assert raised.non_retryable is False
        assert raised.type not in agent_temporal._PERMANENT  # noqa: SLF001
    else:
        assert type(raised).__name__ not in agent_temporal._PERMANENT  # noqa: SLF001


def test_a_stalled_attempt_ends_well_before_the_activity_does() -> None:
    limit = agent_temporal.MODEL_ACTIVITY["start_to_close_timeout"]
    assert REQUEST_TIMEOUT_SECONDS <= limit.total_seconds() * 0.65

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from localization.agents.temporal import localizer
from localization.workers.run import QueueSpec, build_worker
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.localizer_fakes import LocalizerFakes, bound_localizer
from tests.support.prep_fakes import PrepFakes


def fake_activities(fakes: PrepFakes) -> dict[str, object]:
    return {a.__temporal_activity_definition.name: a for a in fakes.activities()}  # type: ignore[attr-defined]


@asynccontextmanager
async def running(workers: list[Worker]) -> AsyncIterator[None]:
    tasks = [asyncio.ensure_future(w.run()) for w in workers]
    await asyncio.sleep(0.2)
    try:
        yield
    finally:
        for w in workers:
            await w.shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def prep_workers(
    client: Client, fakes: PrepFakes, localizing: LocalizerFakes | None = None
) -> AsyncIterator[None]:
    """The real workflow on the `workflows` queue, fake activities on the others.
    The Localizer stage runs for real (agent, validator, durable activities) on a
    scripted model; only the I/O behind its tools and the two activities that touch
    the voice and the object store are stand-ins."""
    acts = fake_activities(fakes)
    localizing = localizing or LocalizerFakes()
    stage = {a.__temporal_activity_definition.name: a for a in localizing.activities()}  # type: ignore[attr-defined]
    agent = AgentPlugin(localizer)
    with bound_localizer(localizing):
        async with running(
            [
                build_worker(client, QueueSpec("workflows", workflows=[VideoLocalizationWorkflow])),
                build_worker(client, QueueSpec("media", activities=[acts["ingest"]])),  # type: ignore[list-item]
                build_worker(client, QueueSpec("stt", activities=[acts["transcribe"]])),  # type: ignore[list-item]
                build_worker(
                    client,
                    QueueSpec(
                        "io",
                        activities=[acts["detect_subtitles"], stage["assemble_script"]],  # type: ignore[list-item]
                        plugins=[agent],
                    ),
                ),
                build_worker(client, QueueSpec("llm", plugins=[agent])),
                build_worker(client, QueueSpec("tts", activities=[stage["voice_rate"]])),  # type: ignore[list-item]
            ]
        ):
            yield

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from temporalio.client import Client
from temporalio.worker import Worker

from localization.workers.run import QueueSpec, build_worker
from localization.workflows.video_localization import VideoLocalizationWorkflow
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
async def prep_workers(client: Client, fakes: PrepFakes) -> AsyncIterator[None]:
    """The real workflow on the `workflows` queue, fake activities on the others."""
    acts = fake_activities(fakes)
    async with running(
        [
            build_worker(client, QueueSpec("workflows", workflows=[VideoLocalizationWorkflow])),
            build_worker(client, QueueSpec("media", activities=[acts["ingest"]])),  # type: ignore[list-item]
            build_worker(client, QueueSpec("stt", activities=[acts["transcribe"]])),  # type: ignore[list-item]
            build_worker(client, QueueSpec("io", activities=[acts["detect_subtitles"]])),  # type: ignore[list-item]
        ]
    ):
        yield

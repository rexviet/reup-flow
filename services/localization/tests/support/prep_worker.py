import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from localization.agents.temporal import dubber, localizer
from localization.workers.run import QueueSpec, build_worker
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.dub_fakes import DubFakes, by_name
from tests.support.localizer_fakes import LocalizerFakes, bound_localizer
from tests.support.prep_fakes import PrepFakes
from tests.support.review_fakes import ReviewFakes


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
    client: Client,
    fakes: PrepFakes,
    localizing: LocalizerFakes | None = None,
    dubbing: DubFakes | None = None,
    tts_limit: int | None = None,
    reviewing: ReviewFakes | None = None,
) -> AsyncIterator[None]:
    """The real workflow on the `workflows` queue, fake activities on the others.
    The Localizer stage runs for real (agent, validator, durable activities) on a
    scripted model; only the I/O behind its tools and the two activities that touch
    the voice and the object store are stand-ins. `dubbing` is the dubbing stage: the
    fakes by default, or any object with the same `activities()` and `bound()`; its
    Dubber runs for real on a scripted model. `tts_limit` is the `tts` queue's
    concurrency. `reviewing` is the layout, render, review and publish steps: fakes that
    pass the first review unless the test says otherwise."""
    acts = fake_activities(fakes)
    review = by_name((reviewing or ReviewFakes()).activities())
    localizing = localizing or LocalizerFakes()
    stage = {a.__temporal_activity_definition.name: a for a in localizing.activities()}  # type: ignore[attr-defined]
    dubbing = dubbing or DubFakes()
    dub = by_name(dubbing.activities())
    agents = [AgentPlugin(localizer), AgentPlugin(dubber)]
    with bound_localizer(localizing), dubbing.bound():
        async with running(
            [
                build_worker(client, QueueSpec("workflows", workflows=[VideoLocalizationWorkflow])),
                build_worker(
                    client,
                    QueueSpec(
                        "media",
                        activities=[
                            acts["ingest"],
                            dub["assemble_audio"],
                            review["layout_captions"],
                        ],  # type: ignore[list-item]
                    ),
                ),
                build_worker(client, QueueSpec("stt", activities=[acts["transcribe"]])),  # type: ignore[list-item]
                build_worker(
                    client,
                    QueueSpec(
                        "io",
                        activities=[
                            acts["detect_subtitles"],  # type: ignore[list-item]
                            stage["assemble_script"],  # type: ignore[list-item]
                            review["publish"],  # type: ignore[list-item]
                            *(
                                dub[n]
                                for n in (
                                    "voice_profile",
                                    "plan_synthesis",
                                    "measure_fit",
                                    "write_dubbed_script",
                                    "commit_dub",
                                )
                            ),
                        ],
                        plugins=agents,
                    ),
                ),
                build_worker(
                    client,
                    QueueSpec("llm", activities=[review["review_render"]], plugins=agents),  # type: ignore[list-item]
                ),
                build_worker(client, QueueSpec("render", activities=[review["render"]])),  # type: ignore[list-item]
                build_worker(
                    client,
                    QueueSpec(
                        "tts",
                        tts_limit,
                        activities=[stage["voice_rate"], dub["synthesize_segments"]],  # type: ignore[list-item]
                        plugins=agents,
                    ),
                ),
            ]
        ):
            yield

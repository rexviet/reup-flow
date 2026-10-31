import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from localization.activities.agent_context import AgentContextActivities
from localization.agents.temporal import (
    dubber,
    dubber_code,
    dubber_repair_agent,
    dubber_single_agent,
    localizer,
    localizer_repair_agent,
    localizer_single_agent,
)
from localization.workers.run import QueueSpec, build_worker
from localization.workflows.video_localization import VideoLocalizationWorkflow
from tests.support.ad_fakes import AdFakes
from tests.support.dub_fakes import DubFakes, by_name
from tests.support.localizer_fakes import LocalizerFakes, bound_localizer
from tests.support.prep_fakes import PrepFakes
from tests.support.review_fakes import ReviewFakes
from tests.support.voice_fakes import VoiceFakes


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
    workflow: type = VideoLocalizationWorkflow,
    classifying: AdFakes | None = None,
    voicing: VoiceFakes | None = None,
) -> AsyncIterator[None]:
    """The real workflow on the `workflows` queue, fake activities on the others.
    The Localizer stage runs for real (agent, validator, durable activities) on a
    scripted model; only the I/O behind its tools and the two activities that touch
    the voice and the object store are stand-ins. `dubbing` is the dubbing stage: the
    fakes by default, or any object with the same `activities()` and `bound()`; its
    Dubber runs for real on a scripted model. `tts_limit` is the `tts` queue's
    concurrency. `reviewing` is the layout, render, review and publish steps: fakes that
    pass the first review unless the test says otherwise. `workflow` is the workflow class to
    run: the real one, or a subclass that changes one of its limits. `classifying` is the
    ad check: the real activity on a stand-in Jev that finds no advertisement unless the
    test says otherwise. `voicing` is the voice check: the real activity on a stand-in
    detector that hears a voice unless the test says otherwise, over the bucket `publish`
    copies from."""
    acts = fake_activities(fakes)
    (classify,) = (classifying or AdFakes()).activities()
    reviewing = reviewing or ReviewFakes()
    review = by_name(reviewing.activities())
    (check_voice,) = (voicing or VoiceFakes()).activities(reviewing.store)
    localizing = localizing or LocalizerFakes()
    stage = {a.__temporal_activity_definition.name: a for a in localizing.activities()}  # type: ignore[attr-defined]
    dubbing = dubbing or DubFakes()
    dub = by_name(dubbing.activities())
    agents = [
        AgentPlugin(a)
        for a in (
            localizer,
            dubber,
            dubber_code,
            localizer_single_agent,
            localizer_repair_agent,
            dubber_single_agent,
            dubber_repair_agent,
        )
    ]
    context = AgentContextActivities(localizing.mode, dubbing.mode)
    with bound_localizer(localizing), dubbing.bound():
        async with running(
            [
                build_worker(client, QueueSpec("workflows", workflows=[workflow])),
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
                            context.localizer_context,
                            context.dubber_context,
                            context.dubber_setup,
                            review["publish"],  # type: ignore[list-item]
                            review["segment_room"],  # type: ignore[list-item]
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
                    QueueSpec(
                        "llm",
                        activities=[
                            review["review_before_render"],  # type: ignore[list-item]
                            review["review_render"],  # type: ignore[list-item]
                            classify,  # type: ignore[list-item]
                        ],
                        plugins=agents,
                    ),
                ),
                build_worker(client, QueueSpec("render", activities=[review["render"]])),  # type: ignore[list-item]
                build_worker(client, QueueSpec("voice_check", activities=[check_voice])),
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

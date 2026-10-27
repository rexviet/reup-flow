"""One process, one `Worker` per task queue.

    python -m localization.workers.run agents   # workflows, llm, io, stt, tts
    python -m localization.workers.run media    # media

Each queue is its own `Worker`, so each has its own concurrency limit. `workflows`
carries only the workflow and depends on nothing external: a VoiceStudio outage
cannot stop a workflow task from being delivered.
"""

import asyncio
import functools
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from localization.activities.agent_context import AgentContextActivities
from localization.activities.assemble_audio import AssembleAudioActivity
from localization.activities.assemble_script import AssembleScriptActivity
from localization.activities.detect_subtitles import DetectSubtitlesActivity
from localization.activities.dub_script import DubScriptActivities
from localization.activities.dubber_ports import StoreDubberPorts
from localization.activities.glossary import GlossaryStore
from localization.activities.ingest import IngestActivity
from localization.activities.layout_captions import LayoutCaptionsActivity
from localization.activities.localizer_ports import StorePorts
from localization.activities.persist import PersistActivity
from localization.activities.publish import PublishActivity, RunStore
from localization.activities.review_render import ReviewRenderActivity
from localization.activities.synthesize import SynthesizeActivities, Synthesizer
from localization.activities.transcribe import TranscribeActivity
from localization.activities.voice_rate import VoiceRateActivity, VoiceRateStore
from localization.agents import dubber as dubber_module
from localization.agents.localizer import bind_ports
from localization.agents.temporal import (
    DUBBER_MODEL_ALIAS,
    bind_model_factory,
    dubber,
    dubber_repair_agent,
    dubber_single_agent,
    localizer,
    localizer_repair_agent,
    localizer_single_agent,
)
from localization.config import ConfigError, Settings, load_settings
from localization.observability.llm_calls import LlmCallRecorder
from localization.observability.llm_usage import Recorder
from localization.observability.tracing import init_tracing
from localization.providers.base import TtsProvider
from localization.providers.jev import jev_for
from localization.providers.llm import model_for, openrouter_client
from localization.providers.registry import Providers, build_media_providers, build_providers
from localization.storage.object_store import ObjectStore
from localization.temporal.client import connect
from localization.workers.health import VoiceStudioHealth
from localization.workflows.video_localization import VideoLocalizationWorkflow

log = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN_SECONDS = 60


@dataclass(frozen=True)
class QueueSpec:
    name: str
    limit: int | None = None
    workflows: Sequence[type] = ()
    activities: Sequence[Callable] = field(default_factory=tuple)
    plugins: Sequence[Any] = ()


def build_worker(client: Client, spec: QueueSpec) -> Worker:
    """The one place a queue's concurrency limit is applied."""
    return Worker(
        client,
        task_queue=spec.name,
        workflows=list(spec.workflows),
        activities=list(spec.activities),
        plugins=list(spec.plugins),
        max_concurrent_activities=spec.limit,
        # On shutdown, in-flight activities get this long to finish before they are
        # cancelled; compose's stop_grace_period is longer, so a deploy does not
        # kill a separation or a transcription halfway.
        graceful_shutdown_timeout=timedelta(seconds=GRACEFUL_SHUTDOWN_SECONDS),
    )


def _database_url(settings: Settings) -> str:
    url = settings.loc_database_url
    return url.get_secret_value() if url is not None else ""


def queue_specs(
    role: str,
    settings: Settings,
    store: ObjectStore,
    providers: Providers,
    engine: Callable[[], str | None] = lambda: None,
    llm_http: httpx.AsyncClient | None = None,
    recorder: Recorder | None = None,
) -> list[QueueSpec]:
    """A queue with nothing to run gets no worker. `engine` names the speech engine
    VoiceStudio reports as active; it is part of a measured voice's identity."""
    if role == "media":
        ingest = IngestActivity(store, providers.demucs)
        assemble_audio = AssembleAudioActivity(
            store,
            loudness_lufs=settings.loudness_lufs,
            true_peak_dbtp=settings.true_peak_dbtp,
        )
        layout = LayoutCaptionsActivity(
            store,
            font_path=settings.caption_font_path,
            cover_color=settings.cover_color,
            cover_opacity=settings.cover_opacity,
        )
        return [
            QueueSpec(
                "media",
                settings.media_concurrency,
                activities=[ingest.ingest, assemble_audio.assemble_audio, layout.layout_captions],
            )
        ]
    if role == "agents":
        transcribe = TranscribeActivity(store, providers.stt)
        detect = DetectSubtitlesActivity(store, providers.ocr)
        database_url = _database_url(settings)
        assemble = AssembleScriptActivity(store)
        voice_rate = VoiceRateActivity(
            providers.tts,
            VoiceRateStore(database_url),
            model=settings.tts_model,
            engine=engine,
        )
        synthesis = SynthesizeActivities(
            store, providers.tts, model=settings.tts_model, engine=engine
        )
        dub = DubScriptActivities(store, synthesis.synthesizer)
        review = ReviewRenderActivity(
            store,
            providers.ocr,
            lambda: model_for("reviewer", settings, llm_http),
            loudness_lufs=settings.loudness_lufs,
            true_peak_dbtp=settings.true_peak_dbtp,
            mode=settings.reviewer_mode,
            jev=functools.cache(lambda: jev_for(settings)),
            recorder=recorder,
            font_path=settings.caption_font_path,
        )
        publish = PublishActivity(store, RunStore(database_url))
        # The agents' model requests (`llm`) and tools (`io`, and the Dubber's
        # `synthesize` on `tts`) are activities of the agents; their plugins register
        # them on each queue's worker.
        # The one-request mode has its own agents (the answer and its repair, for each of
        # the two), so each model request is recorded under its own stage.
        agents = [
            AgentPlugin(a)
            for a in (
                localizer,
                dubber,
                localizer_single_agent,
                localizer_repair_agent,
                dubber_single_agent,
                dubber_repair_agent,
            )
        ]
        agent_context = AgentContextActivities(settings.localizer_mode, settings.dubber_mode)
        return [
            QueueSpec("workflows", workflows=[VideoLocalizationWorkflow]),
            # The Reviewer's model requests are made inside `review_render`, which is why
            # that activity lives here and not on a queue of its own.
            QueueSpec(
                "llm", settings.llm_concurrency, activities=[review.review_render], plugins=agents
            ),
            QueueSpec(
                "io",
                settings.io_concurrency,
                activities=[
                    detect.detect_subtitles,
                    PersistActivity(store).persist_json,
                    assemble.assemble_script,
                    agent_context.localizer_context,
                    agent_context.dubber_context,
                    synthesis.voice_profile,
                    synthesis.plan_synthesis,
                    dub.measure_fit,
                    dub.segment_room,
                    dub.write_dubbed_script,
                    dub.commit_dub,
                    publish.publish,
                ],
                plugins=agents,
            ),
            QueueSpec("stt", settings.stt_concurrency, activities=[transcribe.transcribe]),
            QueueSpec(
                "tts",
                settings.tts_concurrency,
                activities=[voice_rate.voice_rate, synthesis.synthesize_segments],
                plugins=agents,
            ),
        ]
    raise ConfigError(f"unknown worker role {role!r}; expected 'agents' or 'media'")


def runnable(specs: Sequence[QueueSpec]) -> list[QueueSpec]:
    return [s for s in specs if s.workflows or s.activities or s.plugins]


def bind_agent_runtime(
    settings: Settings,
    store: ObjectStore,
    tts: TtsProvider | None = None,
    llm_http: httpx.AsyncClient | None = None,
) -> None:
    """What the agents' activities need on this process: the models that answer their
    requests and the ports their tools use. Refuses to start without them, so a
    missing setting is found at startup and not on the first video."""
    if settings.loc_database_url is None:
        raise ConfigError("LOC_DATABASE_URL is not set")
    model = model_for("localizer", settings, llm_http)
    bind_model_factory(lambda: model)
    bind_ports(StorePorts(store, GlossaryStore(_database_url(settings))))
    if settings.reviewer_mode == "llm":
        # The Reviewer's model is built per review; check now that it can be.
        model_for("reviewer", settings, llm_http)
    elif settings.openrouter_api_key is None:
        raise ConfigError("OPENROUTER_API_KEY is not set")
    dubber_model = model_for("dubber", settings, llm_http)
    bind_model_factory(lambda: dubber_model, DUBBER_MODEL_ALIAS)
    if tts is not None:
        synthesizer = Synthesizer(store, tts, model=settings.tts_model)
        dubber_module.bind_ports(StoreDubberPorts(store, synthesizer))


async def serve(role: str) -> int:
    settings = load_settings()
    init_tracing(settings, f"localization-worker-{role}")
    client = await connect(settings.temporal_address, settings.temporal_namespace)
    if role not in ("agents", "media"):
        raise ConfigError(f"unknown worker role {role!r}; expected 'agents' or 'media'")
    # One HTTP client for every OpenRouter request of this process: its response hook
    # records what each request cost, on the span and in `localization.llm_calls`.
    recorder = (
        LlmCallRecorder(_database_url(settings))
        if role == "agents" and settings.loc_database_url
        else None
    )
    llm_http = openrouter_client(recorder) if role == "agents" else None
    providers = (
        build_providers(settings, llm_http=llm_http)
        if role == "agents"
        else build_media_providers(settings)
    )
    store = ObjectStore.from_settings(settings)
    health = VoiceStudioHealth.from_settings(settings) if role == "agents" else None
    if role == "agents":
        bind_agent_runtime(settings, store, providers.tts, llm_http)
    specs = runnable(
        queue_specs(
            role,
            settings,
            store,
            providers,
            engine=(lambda: health.state.engine) if health is not None else (lambda: None),
            llm_http=llm_http,
            recorder=recorder,
        )
    )
    workers = [build_worker(client, spec) for spec in specs]
    log.info("worker %s serving queues %s", role, [s.name for s in specs])

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    health_task = asyncio.ensure_future(health.run()) if health is not None else None
    code = await supervise(workers, stop)
    if health_task is not None:
        health_task.cancel()
        await asyncio.gather(health_task, return_exceptions=True)
    return code


async def _shutdown(worker: Worker) -> None:
    try:
        await worker.shutdown()
    except Exception:  # noqa: BLE001 - shutting down the rest matters more
        log.exception("a worker did not shut down cleanly")


async def supervise(workers: Sequence[Worker], stop: asyncio.Event) -> int:
    """Runs the workers until `stop` is set (exit 0) or one of them dies (exit 1).

    A dead `Worker.run()` leaves its queue unserved, and the others would keep the
    process alive looking healthy; exiting non-zero lets the container restart."""
    tasks = [asyncio.ensure_future(w.run()) for w in workers]
    stopped = asyncio.ensure_future(stop.wait())
    await asyncio.wait([*tasks, stopped], return_when=asyncio.FIRST_COMPLETED)
    died = [t for t in tasks if t.done()]
    for task in died:
        if task.exception() is not None:
            log.error("a worker stopped: %r", task.exception())
        else:
            log.error("a worker returned before shutdown")
    stopped.cancel()
    # Concurrently: each worker drains for up to the graceful period, and the
    # container's stop_grace_period covers one period, not one per queue.
    await asyncio.gather(*(_shutdown(w) for w in workers))
    await asyncio.gather(*tasks, return_exceptions=True)
    return 1 if died else 0


def main(argv: Sequence[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    if len(argv) != 1:
        print("usage: python -m localization.workers.run agents|media", file=sys.stderr)
        return 2
    return asyncio.run(serve(argv[0]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

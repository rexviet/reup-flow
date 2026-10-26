"""One process, one `Worker` per task queue.

    python -m localization.workers.run agents   # workflows, llm, io, stt, tts
    python -m localization.workers.run media    # media

Each queue is its own `Worker`, so each has its own concurrency limit. `workflows`
carries only the workflow and depends on nothing external: a VoiceStudio outage
cannot stop a workflow task from being delivered.
"""

import asyncio
import logging
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from localization.activities.detect_subtitles import DetectSubtitlesActivity
from localization.activities.ingest import IngestActivity
from localization.activities.persist import PersistActivity
from localization.activities.transcribe import TranscribeActivity
from localization.config import ConfigError, Settings, load_settings
from localization.observability.tracing import init_tracing
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


def build_worker(client: Client, spec: QueueSpec) -> Worker:
    """The one place a queue's concurrency limit is applied."""
    return Worker(
        client,
        task_queue=spec.name,
        workflows=list(spec.workflows),
        activities=list(spec.activities),
        max_concurrent_activities=spec.limit,
        # On shutdown, in-flight activities get this long to finish before they are
        # cancelled; compose's stop_grace_period is longer, so a deploy does not
        # kill a separation or a transcription halfway.
        graceful_shutdown_timeout=timedelta(seconds=GRACEFUL_SHUTDOWN_SECONDS),
    )


def queue_specs(
    role: str, settings: Settings, store: ObjectStore, providers: Providers
) -> list[QueueSpec]:
    """`workflows`, `llm` and `tts` have no activities yet in this build: a queue
    with nothing to run gets no worker."""
    if role == "media":
        ingest = IngestActivity(store, providers.demucs)
        return [QueueSpec("media", settings.media_concurrency, activities=[ingest.ingest])]
    if role == "agents":
        transcribe = TranscribeActivity(store, providers.stt)
        detect = DetectSubtitlesActivity(store, providers.ocr)
        return [
            QueueSpec("workflows", workflows=[VideoLocalizationWorkflow]),
            QueueSpec("llm", settings.llm_concurrency),
            QueueSpec(
                "io",
                settings.io_concurrency,
                activities=[detect.detect_subtitles, PersistActivity(store).persist_json],
            ),
            QueueSpec("stt", settings.stt_concurrency, activities=[transcribe.transcribe]),
            QueueSpec("tts", settings.tts_concurrency),
        ]
    raise ConfigError(f"unknown worker role {role!r}; expected 'agents' or 'media'")


def runnable(specs: Sequence[QueueSpec]) -> list[QueueSpec]:
    return [s for s in specs if s.workflows or s.activities]


async def serve(role: str) -> int:
    settings = load_settings()
    init_tracing(settings, f"localization-worker-{role}")
    client = await connect(settings.temporal_address, settings.temporal_namespace)
    if role not in ("agents", "media"):
        raise ConfigError(f"unknown worker role {role!r}; expected 'agents' or 'media'")
    providers = build_providers(settings) if role == "agents" else build_media_providers(settings)
    specs = runnable(queue_specs(role, settings, ObjectStore.from_settings(settings), providers))
    workers = [build_worker(client, spec) for spec in specs]
    health = VoiceStudioHealth.from_settings(settings) if role == "agents" else None
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

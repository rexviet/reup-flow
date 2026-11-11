"""Each task queue has its own concurrency limit, applied from the settings.

Staying under the limit is only evidence if the same test, with the limit raised,
sees the activities overlap: otherwise a probe that serialises everything would
pass too. Every queue is therefore checked at its limit AND with a control limit.
"""

import asyncio
import dataclasses
import time
import uuid
from types import SimpleNamespace

import pytest
from temporalio import activity
from temporalio.client import Client

from localization.config import Settings
from localization.storage.object_store import ObjectStore
from localization.workers.run import QueueSpec, build_worker, queue_specs
from tests.support.fanout_flow import FanOutWorkflow

TASKS = 8
HOLD_SECONDS = 0.4


def max_overlap(spans: list[tuple[float, float]]) -> int:
    events = sorted([(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans])
    running = peak = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


async def _measure(client: Client, spec: QueueSpec) -> int:
    spans: list[tuple[float, float]] = []

    @activity.defn(name="overlap_probe")
    async def probe(i: int) -> None:
        entered = time.monotonic()
        await asyncio.sleep(HOLD_SECONDS)
        spans.append((entered, time.monotonic()))

    queue = f"{spec.name}-{uuid.uuid4().hex[:6]}"
    activity_worker = build_worker(client, QueueSpec(queue, spec.limit, activities=[probe]))
    workflow_worker = build_worker(client, QueueSpec("fanout", workflows=[FanOutWorkflow]))
    tasks = [asyncio.ensure_future(w.run()) for w in (activity_worker, workflow_worker)]
    try:
        await asyncio.sleep(0.2)
        await client.execute_workflow(
            FanOutWorkflow.run,
            args=[queue, TASKS],
            id=f"fanout-{uuid.uuid4()}",
            task_queue="fanout",
        )
    finally:
        await activity_worker.shutdown()
        await workflow_worker.shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert len(spans) == TASKS
    return max_overlap(spans)


def _specs(settings: Settings) -> dict[str, QueueSpec]:
    providers = SimpleNamespace(
        stt=object(),
        ocr=object(),
        tts=object(),
        demucs=object(),
        vbee_stt=None,
        vbee_tts=None,
        singing=None,
    )
    store = ObjectStore(object(), "b")
    specs = queue_specs("agents", settings, store, providers) + queue_specs(  # type: ignore[arg-type]
        "media",
        settings,
        store,
        providers,  # type: ignore[arg-type]
    )
    return {s.name: s for s in specs}


@pytest.mark.parametrize(
    ("queue", "field", "limit"),
    [
        ("stt", "stt_concurrency", 1),
        ("tts", "tts_concurrency", 2),
        ("media", "media_concurrency", 2),
        ("io", "io_concurrency", 2),
    ],
)
async def test_a_queue_never_runs_more_activities_than_its_limit(
    temporal_client: Client, queue: str, field: str, limit: int
) -> None:
    settings = Settings(**{field: limit})
    spec = _specs(settings)[queue]
    assert spec.limit == limit
    assert await _measure(temporal_client, spec) == limit


async def test_control_a_higher_limit_does_overlap(temporal_client: Client) -> None:
    """The same probe with the limit at 3 must show 3 at once, or the tests above
    could not have told a limit from a serialised probe."""
    spec = dataclasses.replace(_specs(Settings())["stt"], limit=3)
    assert await _measure(temporal_client, spec) == 3


async def test_the_workflows_queue_has_no_limit_and_no_activities() -> None:
    spec = _specs(Settings())["workflows"]
    assert spec.limit is None
    assert not spec.activities
    assert [w.__name__ for w in spec.workflows] == [
        "VideoLocalizationWorkflow",
        "VbeeDubbingWorkflow",
        "RevoiceCaptionWorkflow",
    ]

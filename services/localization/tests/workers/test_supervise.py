import asyncio
import uuid

from temporalio import activity
from temporalio.client import Client

from localization.workers.run import QueueSpec, build_worker, supervise
from tests.support.fanout_flow import FanOutWorkflow


class FakeWorker:
    def __init__(self, *, fail_after: float | None = None, drain_seconds: float = 0.0) -> None:
        self.fail_after = fail_after
        self.drain_seconds = drain_seconds
        self.stopped = asyncio.Event()
        self.shut_down = False

    async def run(self) -> None:
        if self.fail_after is not None:
            await asyncio.sleep(self.fail_after)
            raise RuntimeError("poller crashed")
        await self.stopped.wait()

    async def shutdown(self) -> None:
        await asyncio.sleep(self.drain_seconds)
        self.shut_down = True
        self.stopped.set()


async def test_a_dead_worker_ends_the_process_with_a_failure_and_stops_the_rest() -> None:
    healthy, dying = FakeWorker(), FakeWorker(fail_after=0.05)
    code = await asyncio.wait_for(supervise([healthy, dying], asyncio.Event()), timeout=5)
    assert code == 1
    assert healthy.shut_down


async def test_control_a_requested_stop_is_a_clean_exit() -> None:
    workers = [FakeWorker(), FakeWorker()]
    stop = asyncio.Event()
    task = asyncio.ensure_future(supervise(workers, stop))
    await asyncio.sleep(0.05)
    stop.set()
    assert await asyncio.wait_for(task, timeout=5) == 0
    assert all(w.shut_down for w in workers)


async def test_shutdown_lets_a_running_activity_finish(temporal_client: Client) -> None:
    """Without a grace period a deploy cancels an in-flight separation or
    transcription instead of letting it finish."""
    finished: list[str] = []
    started = asyncio.Event()

    @activity.defn(name="overlap_probe")
    async def slow(i: int) -> None:
        started.set()
        try:
            await asyncio.sleep(1.0)
            finished.append("completed")
        except asyncio.CancelledError:
            finished.append("cancelled")
            raise

    queue = f"grace-{uuid.uuid4().hex[:6]}"
    activities = build_worker(temporal_client, QueueSpec(queue, activities=[slow]))
    flows = build_worker(temporal_client, QueueSpec("fanout", workflows=[FanOutWorkflow]))
    tasks = [asyncio.ensure_future(w.run()) for w in (activities, flows)]
    await asyncio.sleep(0.2)
    run = asyncio.ensure_future(
        temporal_client.execute_workflow(
            FanOutWorkflow.run, args=[queue, 1], id=f"g-{uuid.uuid4()}", task_queue="fanout"
        )
    )
    await asyncio.wait_for(started.wait(), 10)
    await activities.shutdown()
    await asyncio.wait_for(run, 20)
    await flows.shutdown()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert finished == ["completed"]


def test_the_grace_period_is_shorter_than_composes_stop_grace_period() -> None:
    from localization.workers.run import GRACEFUL_SHUTDOWN_SECONDS

    assert 0 < GRACEFUL_SHUTDOWN_SECONDS < 90


async def test_workers_drain_at_the_same_time_not_one_after_another() -> None:
    """Each worker may take up to the graceful period to drain; one after another,
    three busy queues would need three periods against a one-period stop budget."""
    workers = [FakeWorker(drain_seconds=0.5) for _ in range(3)]
    stop = asyncio.Event()
    task = asyncio.ensure_future(supervise(workers, stop))
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    stop.set()
    assert await asyncio.wait_for(task, timeout=5) == 0
    elapsed = asyncio.get_running_loop().time() - started
    assert all(w.shut_down for w in workers)
    assert elapsed < 1.0, f"shutdowns ran one after another ({elapsed:.2f}s for 3 x 0.5s)"

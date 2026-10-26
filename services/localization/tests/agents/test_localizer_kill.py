"""A worker killed for real (SIGKILL) between a glossary write and its answer."""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment

from localization.contracts import LocalizationResult
from localization.temporal.client import connect
from tests.support.inputs import make_input
from tests.support.kill_worker import TERM

SERVICE = Path(__file__).resolve().parents[2]


@contextmanager
def worker_process(address: str, conninfo: str, marker: Path) -> Iterator[subprocess.Popen]:
    env = {**os.environ, "PYTHONPATH": f"{SERVICE / 'src'}:{SERVICE}"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.support.kill_worker", address, conninfo, str(marker)],
        cwd=SERVICE,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        # Only the process this test started, and only if it is still there.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


async def test_a_worker_killed_after_a_glossary_write_leaves_one_row_when_work_resumes(
    glossary_db, tmp_path: Path
) -> None:
    info, conn = glossary_db
    marker = tmp_path / "first-write-done"
    env = await WorkflowEnvironment.start_local()
    try:
        address = env.client.service_client.config.target_host
        client = await connect(address)
        with worker_process(address, info, marker) as first:
            handle = await client.start_workflow(
                "VideoLocalizationWorkflow",
                make_input(),
                id=f"video-7001-g1-{uuid.uuid4()}",
                task_queue="workflows",
                result_type=LocalizationResult,
            )
            await asyncio.to_thread(
                _wait_for,
                lambda: (
                    conn.execute("SELECT count(*) FROM localization.glossary").fetchone()[0] == 1
                ),
                60,
                "the glossary row to be written",
            )
            assert marker.exists()
            first.kill()  # SIGKILL: the tool never answers
            first.wait()
            with worker_process(address, info, marker):
                result = await asyncio.wait_for(handle.result(), timeout=120)
        history = await handle.fetch_history()
    finally:
        await env.shutdown()

    assert isinstance(result, LocalizationResult)
    assert result.script is not None
    rows = conn.execute("SELECT term_zh, term_vi FROM localization.glossary").fetchall()
    assert rows == [TERM]
    started = [
        e.activity_task_started_event_attributes.attempt
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
    ]
    assert max(started) >= 2, "the killed tool call was never retried"
    model_requests = [
        e
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        and e.activity_task_scheduled_event_attributes.activity_type.name
        == "agent__localizer__model_request"
    ]
    assert len(model_requests) == 3, "a finished model request was run again after the kill"

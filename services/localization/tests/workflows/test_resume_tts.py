"""A text is sent to the TTS provider at most once, however the run is interrupted.

The workers here are real processes that are really killed (SIGKILL), over a bucket
that is a directory and a provider that appends every call to a file, so the count
survives the kill. The cache is the real one: `cache/tts/{hash}.wav`, written whole
or not at all.
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from localization.activities.script_document import read_script
from localization.activities.synthesize import tts_hash
from localization.contracts import DubSegmentsDocument, LocalizationResult
from localization.storage.object_store import ObjectStore, tts_cache_key, work_key
from localization.temporal.client import connect
from tests.support.dub_worker import BUCKET, CLIPS_PER_TWO_BATCHES
from tests.support.file_s3 import FileS3
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.real_dubbing import RealDubbing
from tests.support.tts_fakes import FakeTts, put_script, segment

SERVICE = Path(__file__).resolve().parents[2]
SPEC = make_input()
MODEL = "tts-1"
ENGINE = "engine-a"
# Five batches of ten.
TEXTS = [f"câu số {i} của video" for i in range(50)]
TTS_CONCURRENCY = 2


def hash_of(text: str, *, model: str = MODEL, engine: str | None = ENGINE) -> str:
    return tts_hash(SPEC.voice, model=model, engine=engine, text_vi=text)


def seeded(state: Path, texts: list[str]) -> tuple[FileS3, ObjectStore]:
    s3 = FileS3(state / "bucket")
    store = ObjectStore(s3, BUCKET)
    put_script(store, [segment(i, t) for i, t in enumerate(texts)])
    return s3, store


@contextmanager
def worker(address: str, state: Path, mode: str, limit: int) -> Iterator[subprocess.Popen]:
    env = {**os.environ, "PYTHONPATH": f"{SERVICE / 'src'}:{SERVICE}"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.support.dub_worker", address, str(state), mode, str(limit)],
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
            proc.kill()
            proc.wait()


def wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def calls(state: Path) -> Counter:
    log = state / "calls.log"
    return Counter(log.read_text().splitlines()) if log.exists() else Counter()


async def start(client: Client):
    return await client.start_workflow(
        "VideoLocalizationWorkflow",
        SPEC,
        id=f"video-7001-g1-{uuid.uuid4()}",
        task_queue="workflows",
        result_type=LocalizationResult,
    )


# --- a kill between two batches ---------------------------------------------------------


async def test_a_worker_killed_after_two_batches_costs_no_repeated_call(tmp_path: Path) -> None:
    s3, _ = seeded(tmp_path, TEXTS)
    env = await WorkflowEnvironment.start_local()
    try:
        address = env.client.service_client.config.target_host
        client = await connect(address)
        with worker(address, tmp_path, "stall", limit=1) as first:
            handle = await start(client)
            await asyncio.to_thread(
                wait_for, lambda: (tmp_path / "marker").exists(), 60, "the provider to stall"
            )
            cached_at_kill = {k for k in s3.keys("cache/tts/")}
            first.kill()
            first.wait()
            with worker(address, tmp_path, "run", limit=1):
                result = await asyncio.wait_for(handle.result(), timeout=150)
    finally:
        await env.shutdown()

    # Two full batches were done when the process died, and nothing was in flight.
    assert len(cached_at_kill) == CLIPS_PER_TWO_BATCHES
    assert {c for c in calls(tmp_path)} == set(TEXTS)
    counts = calls(tmp_path)
    assert {t: n for t, n in counts.items() if n != 1} == {}, "a text was sent twice"
    assert len(counts) == len(TEXTS)
    assert isinstance(result, LocalizationResult) and result.dub is not None
    assert s3.keys("cache/tts/") == {tts_cache_key(hash_of(t)) for t in TEXTS}


# --- a kill between the provider's answer and the write ------------------------------------


async def test_a_worker_killed_between_the_answer_and_the_write_repeats_only_what_was_in_flight(
    tmp_path: Path,
) -> None:
    s3, _ = seeded(tmp_path, TEXTS)
    env = await WorkflowEnvironment.start_local()
    try:
        address = env.client.service_client.config.target_host
        client = await connect(address)
        with worker(address, tmp_path, "killput", limit=TTS_CONCURRENCY) as first:
            handle = await start(client)
            await asyncio.to_thread(wait_for, lambda: first.poll() is not None, 60, "the kill")
            assert first.returncode == -9, "the worker was not killed by the injected SIGKILL"
            cached = s3.keys("cache/tts/")
            called_before = calls(tmp_path)
            killed_on = (tmp_path / "marker").read_text()
            with worker(address, tmp_path, "run", limit=TTS_CONCURRENCY):
                result = await asyncio.wait_for(handle.result(), timeout=150)
    finally:
        await env.shutdown()

    # The calls that were in flight: asked of the provider, no clip written.
    in_flight = sorted(t for t in called_before if tts_cache_key(hash_of(t)) not in cached)
    in_flight_hashes = [hash_of(t) for t in in_flight]
    assert hash_of(next(t for t in in_flight if hash_of(t) == killed_on)) == killed_on
    assert 1 <= len(in_flight) <= TTS_CONCURRENCY, f"in flight at the kill: {in_flight_hashes}"
    assert len(cached) >= 24, "the kill came before the cache had filled"

    counts = calls(tmp_path)
    repeated = sorted(t for t, n in counts.items() if n > 1)
    assert repeated == in_flight, f"repeated {repeated}, but only {in_flight} were in flight"
    assert all(counts[t] == 2 for t in in_flight)
    assert all(counts[t] == 1 for t in TEXTS if t not in in_flight)
    assert isinstance(result, LocalizationResult) and result.dub is not None
    assert s3.keys("cache/tts/") == {tts_cache_key(hash_of(t)) for t in TEXTS}


# --- in one process: the rest of the contract ------------------------------------------------


class SlowTts(FakeTts):
    """Takes long enough that two batches really run at the same time."""

    async def synthesize(self, text, voice):
        await asyncio.sleep(0.15)
        return await super().synthesize(text, voice)


async def run_real(client: Client, store: ObjectStore, tts, *, limit=TTS_CONCURRENCY, **kw):
    dubbing = RealDubbing(store, tts, model=MODEL, engine=ENGINE, **kw)
    async with prep_workers(
        client, PrepFakes(), LocalizerFakes(), dubbing=dubbing, tts_limit=limit
    ):
        handle = await start(client)
        return await asyncio.wait_for(handle.result(), timeout=60)


async def test_a_text_said_twice_in_different_batches_is_sent_once(
    temporal_client: Client, tmp_path: Path
) -> None:
    """Batches run side by side, so only the plan can keep one text out of two of
    them: segment 10, the first of the second batch, repeats segment 0, the first of
    the first batch, and both batches reach it at the same moment."""
    texts = [f"câu số {i} của video" for i in range(10)] + ["câu số 0 của video"]
    texts += [f"câu số {i} của video" for i in range(11, 15)]
    _, store = seeded(tmp_path, texts)
    tts = SlowTts()
    result = await run_real(temporal_client, store, tts)
    assert isinstance(result, LocalizationResult) and result.dub is not None
    assert max(tts.calls.values()) == 1
    assert tts.total == 14
    doc = DubSegmentsDocument.model_validate_json(
        store.get_bytes(result.dub.segments_key, produced_by="dub_script")
    )
    # Every segment is in the document, the repeated ones pointing at one clip.
    assert [s.id for s in doc.segments] == list(range(15))
    assert doc.segments[10].tts_key == doc.segments[0].tts_key


async def test_a_second_run_over_the_same_script_makes_no_provider_call(
    temporal_client: Client, tmp_path: Path
) -> None:
    _, store = seeded(tmp_path, TEXTS[:12])
    tts = FakeTts()
    await run_real(temporal_client, store, tts)
    first = tts.total
    await run_real(temporal_client, store, tts)
    assert first == 12
    assert tts.total == 12


async def test_a_line_the_dubber_rewrites_costs_one_call_and_is_the_text_that_is_dubbed(
    temporal_client: Client, tmp_path: Path
) -> None:
    """The long lines do not fit a one-second slot. The Dubber speaks its shorter
    version (a call of its own, with its own hash); the second pass over the dubbed
    script finds that clip in the cache instead of asking again, so the rewritten
    text is sent to the provider exactly once."""
    long_lines = [f"một câu rất dài dòng số {i} cần rút gọn" for i in range(3)]
    texts = ["ngắn"] + long_lines
    s3 = FileS3(tmp_path / "bucket")
    store = ObjectStore(s3, BUCKET)
    put_script(store, [segment(i, t, slot_ms=1000) for i, t in enumerate(texts)])
    tts = FakeTts(seconds_per_char=0.05)
    result = await run_real(temporal_client, store, tts)
    assert isinstance(result, LocalizationResult) and result.dub is not None

    rewritten = [f"câu ngắn {i}.0" for i in (1, 2, 3)]
    assert max(tts.calls.values()) == 1, dict(tts.calls)
    assert set(tts.calls) == {*texts, *rewritten}
    final = read_script(store, result.dub.script_key)
    assert [s.text_vi for s in final.segments] == ["ngắn", *rewritten]
    doc = DubSegmentsDocument.model_validate_json(
        store.get_bytes(work_key("7001", 0, "dub-segments.json"), produced_by="dub_script")
    )
    assert [s.text_vi for s in doc.segments] == [s.text_vi for s in final.segments]
    assert all(store.exists(s.tts_key) for s in doc.segments)


async def test_changing_the_model_between_runs_speaks_everything_again(
    temporal_client: Client, tmp_path: Path
) -> None:
    _, store = seeded(tmp_path, TEXTS[:5])
    tts = FakeTts()
    await run_real(temporal_client, store, tts)
    assert tts.total == 5
    other = RealDubbing(store, tts, model="tts-2", engine=ENGINE)
    async with prep_workers(
        temporal_client, PrepFakes(), LocalizerFakes(), dubbing=other, tts_limit=2
    ):
        handle = await start(temporal_client)
        await asyncio.wait_for(handle.result(), timeout=60)
    assert tts.total == 10


async def test_changing_the_engine_between_runs_speaks_everything_again(
    temporal_client: Client, tmp_path: Path
) -> None:
    _, store = seeded(tmp_path, TEXTS[:5])
    tts = FakeTts()
    await run_real(temporal_client, store, tts)
    other = RealDubbing(store, tts, model=MODEL, engine="engine-b")
    async with prep_workers(
        temporal_client, PrepFakes(), LocalizerFakes(), dubbing=other, tts_limit=2
    ):
        handle = await start(temporal_client)
        await asyncio.wait_for(handle.result(), timeout=60)
    assert tts.total == 10


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)

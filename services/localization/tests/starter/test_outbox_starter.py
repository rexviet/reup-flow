"""The outbox starter against a real Postgres (the downloader's real schema and
outbox migration, in a throwaway database) and a real Temporal server."""

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError

from localization.config import Settings
from localization.providers.voices import VoiceCatalog
from localization.starter.outbox_starter import (
    BACKOFF_BASE_SECONDS,
    OutboxStarter,
    Outcome,
    backoff_seconds,
    workflow_id,
)
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.scratch_db import INIT, OUTBOX, scratch_database
from tests.support.stub_server import Scenario, StubServer

BUCKET = "reup-media"


@pytest.fixture
def db() -> Iterator[tuple[str, psycopg.Connection]]:
    yield from scratch_database(INIT, OUTBOX)


def add_video(
    conn: psycopg.Connection,
    aweme_id: str,
    *,
    path: str | None = None,
    channel: str = "chan_1",
    generation: int = 1,
    due_in_seconds: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO videos (aweme_id, sec_user_id, create_time, storage_path, status) "
        "VALUES (%s, %s, now(), %s, 'done')",
        (aweme_id, channel, path if path is not None else f"s3://{BUCKET}/source/{aweme_id}.mp4"),
    )
    conn.execute(
        "INSERT INTO localization_outbox (aweme_id, generation, next_attempt_at) "
        "VALUES (%s, %s, now() + make_interval(secs => %s))",
        (aweme_id, generation, due_in_seconds),
    )


def row(conn: psycopg.Connection, aweme_id: str) -> dict:
    cursor = conn.execute(
        "SELECT state, generation, workflow_id, dispatch_attempts, last_error, "
        "next_attempt_at > now() AS waiting, dispatched_at FROM localization_outbox "
        "WHERE aweme_id = %s",
        (aweme_id,),
    )
    names = [c.name for c in cursor.description]
    return dict(zip(names, cursor.fetchone(), strict=True))


@dataclass
class Env:
    settings: Settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(Settings(voice_id="voice-a", s3_bucket=BUCKET))


class CountingClient:
    """Forwards to the real client and records every `start_workflow`."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self.starts: list[str] = []

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(self._client, name)

    async def start_workflow(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.starts.append(kwargs["id"])
        return await self._client.start_workflow(*args, **kwargs)


def starter(info: str, client: object, env: Env) -> OutboxStarter:
    return OutboxStarter(info, client, load=lambda: env.settings, voices=lambda s: None)  # type: ignore[arg-type]


async def drain(s: OutboxStarter, limit: int = 20) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for _ in range(limit):
        outcome = await s.run_once()
        outcomes.append(outcome)
        if outcome in (Outcome.IDLE, Outcome.THROTTLED):
            break
    return outcomes


async def started_input(client: Client, id_: str) -> dict:
    history = await client.get_workflow_handle(id_).fetch_history()
    payload = history.events[0].workflow_execution_started_event_attributes.input.payloads[0]
    return json.loads(payload.data)


# --- ids and duplicates -----------------------------------------------------------


async def test_two_starters_start_each_generation_exactly_once(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    ids = [str(7000 + i) for i in range(6)]
    for aweme_id in ids:
        add_video(conn, aweme_id)
    client = CountingClient(temporal_client)
    a, b = starter(info, client, env), starter(info, client, env)
    await asyncio.gather(drain(a), drain(b))

    assert sorted(client.starts) == sorted(workflow_id(i, 1) for i in ids)
    assert len(client.starts) == len(set(client.starts)) == 6
    for aweme_id in ids:
        got = row(conn, aweme_id)
        assert (got["state"], got["workflow_id"]) == ("dispatched", workflow_id(aweme_id, 1))
        assert got["dispatched_at"] is not None


async def test_a_row_started_twice_is_one_workflow(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    """A crash between `start_workflow` and the settle makes the row due again."""
    info, conn = db
    add_video(conn, "7001")
    s = starter(info, temporal_client, env)
    assert await s.run_once() == Outcome.DISPATCHED
    conn.execute("UPDATE localization_outbox SET state='pending', next_attempt_at=now()")
    assert await s.run_once() == Outcome.DISPATCHED
    count = await temporal_client.count_workflows("WorkflowType='VideoLocalizationWorkflow'")
    assert count.count == 1
    assert row(conn, "7001")["state"] == "dispatched"


async def test_bumping_the_generation_runs_a_finished_video_again(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    add_video(conn, "7001")
    s = starter(info, temporal_client, env)
    async with prep_workers(temporal_client, PrepFakes()):
        assert await s.run_once() == Outcome.DISPATCHED
        first = temporal_client.get_workflow_handle(workflow_id("7001", 1))
        await asyncio.wait_for(first.result(), 30)
        assert (await first.describe()).status == WorkflowExecutionStatus.COMPLETED

        conn.execute(
            "UPDATE localization_outbox SET generation = generation + 1, state = 'pending', "
            "next_attempt_at = now(), dispatched_at = NULL"
        )
        assert await s.run_once() == Outcome.DISPATCHED
        second = temporal_client.get_workflow_handle(workflow_id("7001", 2))
        await asyncio.wait_for(second.result(), 30)

    assert row(conn, "7001")["workflow_id"] == workflow_id("7001", 2)
    assert first.id != second.id
    assert (await started_input(temporal_client, second.id))["generation"] == 2


async def test_a_generation_bumped_during_the_start_is_not_overwritten(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    add_video(conn, "7001")
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(CountingClient):
        async def start_workflow(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            entered.set()
            await release.wait()
            return await super().start_workflow(*args, **kwargs)

    s = starter(info, Slow(temporal_client), env)
    task = asyncio.ensure_future(s.run_once())
    await asyncio.wait_for(entered.wait(), 10)
    # If the starter still held the row lock this would wait for it, and the
    # starter waits for us: fail fast instead of deadlocking.
    conn.execute("SET lock_timeout = '2s'")
    conn.execute(
        "UPDATE localization_outbox SET generation = 2, state = 'pending', next_attempt_at = now()"
    )
    release.set()
    await task
    got = row(conn, "7001")
    assert (got["state"], got["generation"], got["workflow_id"]) == ("pending", 2, None)
    assert await s.run_once() == Outcome.DISPATCHED
    assert row(conn, "7001")["workflow_id"] == workflow_id("7001", 2)


async def test_no_row_lock_is_held_while_the_workflow_is_started(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    add_video(conn, "7001", due_in_seconds=0)
    add_video(conn, "7002", due_in_seconds=1)
    entered, release = asyncio.Event(), asyncio.Event()

    class Slow(CountingClient):
        async def start_workflow(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            entered.set()
            await release.wait()
            return await super().start_workflow(*args, **kwargs)

    first = asyncio.ensure_future(starter(info, Slow(temporal_client), env).run_once())
    await asyncio.wait_for(entered.wait(), 10)
    try:
        with psycopg.connect(info, autocommit=True) as other:
            other.execute("SET lock_timeout = '1s'")
            # Would block, and then fail on the lock timeout, if the claim were
            # still open around the network call.
            other.execute(
                "SELECT 1 FROM localization_outbox WHERE aweme_id = '7001' FOR UPDATE NOWAIT"
            )
    finally:
        release.set()
        await first
    assert row(conn, "7001")["state"] == "dispatched"


# --- eligibility, errors, backoff --------------------------------------------------


@pytest.mark.parametrize(
    ("path", "channel", "why"),
    [
        ("/data/7001.mp4", "chan_1", "not an s3:// URL"),
        ("s3://other-bucket/source/7001.mp4", "chan_1", "different bucket"),
        (f"s3://{BUCKET}/work/7001.mp4", "chan_1", "source/"),
        (f"s3://{BUCKET}/source/../x.mp4", "chan_1", "source/"),
        (f"s3://{BUCKET}/source/7001.mp4", "bad channel!", "valid workflow input"),
        (None, "chan_1", "not an s3:// URL"),
    ],
)
async def test_a_row_that_can_never_be_valid_is_rejected_and_the_next_row_still_runs(
    db: tuple[str, psycopg.Connection],
    temporal_client: Client,
    env: Env,
    path: str | None,
    channel: str,
    why: str,
) -> None:
    info, conn = db
    add_video(conn, "7001", path=path if path is not None else "", channel=channel)
    if path is None:
        conn.execute("UPDATE videos SET storage_path = NULL")
    add_video(conn, "7002", due_in_seconds=1)
    client = CountingClient(temporal_client)
    await asyncio.sleep(1.1)
    outcomes = await drain(starter(info, client, env))

    assert outcomes[:2] == [Outcome.REJECTED, Outcome.DISPATCHED]
    bad = row(conn, "7001")
    assert bad["state"] == "rejected"
    assert why in bad["last_error"]
    assert row(conn, "7002")["state"] == "dispatched"
    assert client.starts == [workflow_id("7002", 1)]


async def test_a_row_without_a_video_is_rejected(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    conn.execute(
        "ALTER TABLE localization_outbox DROP CONSTRAINT localization_outbox_aweme_id_fkey"
    )
    conn.execute("INSERT INTO localization_outbox (aweme_id) VALUES ('7001')")
    assert await starter(info, temporal_client, env).run_once() == Outcome.REJECTED
    assert row(conn, "7001")["state"] == "rejected"


class FailingClient(CountingClient):
    async def start_workflow(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        self.starts.append(kwargs["id"])
        raise RPCError("unavailable", 14, b"")  # type: ignore[arg-type]


async def test_a_row_that_fails_to_dispatch_five_times_is_dead(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    add_video(conn, "7001")
    s = starter(info, FailingClient(temporal_client), env)
    waits: list[float] = []
    for attempt in range(1, 6):
        conn.execute("UPDATE localization_outbox SET next_attempt_at = now()")
        assert await s.run_once() == Outcome.FAILED
        got = row(conn, "7001")
        assert got["dispatch_attempts"] == attempt
        assert got["last_error"].startswith("RPCError")
        if attempt < 5:
            assert got["state"] == "pending" and got["waiting"] is True
            waits.append(
                conn.execute(
                    "SELECT extract(epoch FROM next_attempt_at - now()) FROM localization_outbox"
                ).fetchone()[0]
            )
    assert row(conn, "7001")["state"] == "dead"
    # Exponential: 30 s, then doubling. Literals, so a change to the formula cannot
    # move the expectation with it.
    assert [round(w / 10) for w in waits] == [3, 6, 12, 24]
    assert [backoff_seconds(i) for i in range(4)] == [30, 60, 120, 240]
    assert backoff_seconds(0) == BACKOFF_BASE_SECONDS
    assert backoff_seconds(20) == 3600  # capped
    # A dead row is never picked again.
    conn.execute("UPDATE localization_outbox SET next_attempt_at = now()")
    assert await s.run_once() == Outcome.IDLE


async def test_a_failing_row_does_not_block_the_rows_behind_it(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    add_video(conn, "7001")
    add_video(conn, "7002", due_in_seconds=1)
    await asyncio.sleep(1.1)

    class FailsFirst(CountingClient):
        async def start_workflow(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            if kwargs["id"] == workflow_id("7001", 1):
                raise RPCError("unavailable", 14, b"")  # type: ignore[arg-type]
            return await super().start_workflow(*args, **kwargs)

    outcomes = await drain(starter(info, FailsFirst(temporal_client), env))
    assert outcomes[:2] == [Outcome.FAILED, Outcome.DISPATCHED]
    assert row(conn, "7002")["state"] == "dispatched"


# --- throttle --------------------------------------------------------------------


async def test_nothing_is_started_while_max_open_workflows_are_running(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    env.settings = Settings(voice_id="voice-a", s3_bucket=BUCKET, max_open_workflows=2)
    for i in range(4):
        add_video(conn, str(7001 + i))
    client = CountingClient(temporal_client)
    s = starter(info, client, env)
    for expected in (1, 2):
        assert await s.run_once() == Outcome.DISPATCHED
        for _ in range(100):  # visibility is eventually consistent
            if (
                await temporal_client.count_workflows("ExecutionStatus='Running'")
            ).count >= expected:
                break
            await asyncio.sleep(0.1)
    assert await s.run_once() == Outcome.THROTTLED
    assert len(client.starts) == 2
    pending = conn.execute("SELECT count(*) FROM localization_outbox WHERE state='pending'")
    assert pending.fetchone() == (2,)
    # A throttled pass claims nothing: the rows behind are still due now.
    due = conn.execute(
        "SELECT count(*) FROM localization_outbox "
        "WHERE state='pending' AND next_attempt_at <= now()"
    )
    assert due.fetchone() == (2,)


async def test_control_below_the_limit_workflows_are_started(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    for i in range(3):
        add_video(conn, str(7001 + i))
    client = CountingClient(temporal_client)
    await drain(starter(info, client, env))
    assert len(client.starts) == 3


# --- voice and language are read at start --------------------------------------------


async def test_changing_the_voice_only_affects_workflows_started_afterwards(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    add_video(conn, "7001")
    add_video(conn, "7002", due_in_seconds=1)
    await asyncio.sleep(1.1)
    s = starter(info, temporal_client, env)
    assert await s.run_once() == Outcome.DISPATCHED
    env.settings = Settings(voice_id="voice-b", s3_bucket=BUCKET, target_lang="en")
    assert await s.run_once() == Outcome.DISPATCHED

    first = await started_input(temporal_client, workflow_id("7001", 1))
    second = await started_input(temporal_client, workflow_id("7002", 1))
    assert (first["voice"]["voice_id"], first["target_lang"]) == ("voice-a", "vi")
    assert (second["voice"]["voice_id"], second["target_lang"]) == ("voice-b", "en")
    # The first workflow's input is unchanged now that the setting differs.
    assert (await started_input(temporal_client, workflow_id("7001", 1)))["voice"]["voice_id"] == (
        "voice-a"
    )


async def test_the_input_carries_the_video_channel_and_generation(
    db: tuple[str, psycopg.Connection], temporal_client: Client, env: Env
) -> None:
    info, conn = db
    add_video(conn, "7001", channel="MS4wLjAB-xyz_1", generation=3)
    assert await starter(info, temporal_client, env).run_once() == Outcome.DISPATCHED
    got = await started_input(temporal_client, workflow_id("7001", 3))
    assert got["aweme_id"] == "7001"
    assert got["channel_id"] == "MS4wLjAB-xyz_1"
    assert got["generation"] == 3
    assert got["video_key"] == "source/7001.mp4"
    assert got["voice"] == {
        "provider": "voicestudio",
        "voice_id": "voice-a",
        "settings": {"speed": 1.0, "instructions": None},
    }


# --- the voice must exist ---------------------------------------------------------------


def _catalog_starter(
    info: str, client: Client, stub: StubServer, monkeypatch: pytest.MonkeyPatch, voice_id: str
) -> OutboxStarter:
    monkeypatch.setenv("VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS", "127.0.0.0/8")
    settings = Settings(voice_id=voice_id, s3_bucket=BUCKET, voicestudio_url=stub.url)
    catalog = VoiceCatalog.from_settings(settings)
    return OutboxStarter(info, client, load=lambda: settings, voices=lambda s: catalog)


async def test_a_voice_the_provider_does_not_list_is_never_dispatched(
    db: tuple[str, psycopg.Connection],
    temporal_client: Client,
    stub: StubServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info, conn = db
    add_video(conn, "7001")
    s = _catalog_starter(info, temporal_client, stub, monkeypatch, "no-such-voice")
    assert await s.run_once() == Outcome.CONFIG_ERROR
    got = row(conn, "7001")
    # Nothing was claimed or counted against the row.
    assert (got["state"], got["dispatch_attempts"], got["waiting"]) == ("pending", 0, False)
    assert (await temporal_client.count_workflows()).count == 0


async def test_any_voice_the_provider_lists_works(
    db: tuple[str, psycopg.Connection],
    temporal_client: Client,
    stub: StubServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info, conn = db
    stub.scenario = Scenario(json_body={"voices": [{"voice_id": "anything-at-all"}]})
    add_video(conn, "7001")
    s = _catalog_starter(info, temporal_client, stub, monkeypatch, "anything-at-all")
    assert await s.run_once() == Outcome.DISPATCHED
    got = await started_input(temporal_client, workflow_id("7001", 1))
    assert got["voice"]["voice_id"] == "anything-at-all"


async def test_an_unreachable_provider_pauses_dispatch_without_touching_the_row(
    db: tuple[str, psycopg.Connection],
    temporal_client: Client,
    stub: StubServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info, conn = db
    stub.scenario = Scenario(status=503)
    add_video(conn, "7001")
    s = _catalog_starter(info, temporal_client, stub, monkeypatch, "voice-a")
    assert await s.run_once() == Outcome.CONFIG_ERROR
    assert row(conn, "7001")["dispatch_attempts"] == 0
    stub.scenario = Scenario()
    assert await s.run_once() == Outcome.DISPATCHED


def test_workflow_id_is_the_video_and_the_generation() -> None:
    assert workflow_id("7001", 4) == "video-7001-g4"

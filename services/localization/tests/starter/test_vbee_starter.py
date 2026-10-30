"""The starter in the Vbee pipeline (LOCALIZATION_PIPELINE=vbee): it starts the Vbee
dubbing workflow under the same id, names the voice by a marker only, checks the Vbee
settings without calling Vbee or VoiceStudio, and counts both workflows against one limit.
Real Postgres (the downloader's schema) and a real Temporal server."""

import asyncio

import psycopg
from pydantic import SecretStr
from temporalio.client import Client

from localization.config import Settings
from localization.contracts.vbee_flow import VBEE_CONFIGURED_VOICE
from localization.starter.outbox_starter import OutboxStarter, Outcome, workflow_id
from tests.starter.test_outbox_starter import BUCKET, add_video, row, started_input
from tests.starter.test_outbox_starter import db as _scratch_db
from tests.support.stub_server import StubServer

db = _scratch_db

CANARY = "vbee-voice-starter-canary-41f2"


def vbee_settings(stub: StubServer | None = None, **more: object) -> Settings:
    values: dict[str, object] = {
        "localization_pipeline": "vbee",
        "s3_bucket": BUCKET,
        "vbee_app_id": SecretStr("app"),
        "vbee_api_token": SecretStr("token"),
        "vbee_voice_id": SecretStr(CANARY),
        # A VoiceStudio setup left in place: the Vbee pipeline must not ask it anything.
        "voice_id": "voice-a",
        "voicestudio_allow_private_http_cidrs": "127.0.0.0/8",
    }
    if stub is not None:
        values["voicestudio_url"] = stub.url
    values.update(more)
    return Settings(**values)  # type: ignore[arg-type]


async def test_a_vbee_run_is_the_vbee_workflow_under_the_same_id_with_the_marker_voice(
    db: tuple[str, psycopg.Connection], temporal_client: Client, stub: StubServer
) -> None:
    info, conn = db
    add_video(conn, "7001", generation=2)
    settings = vbee_settings(stub)
    # The default wiring, which checks a VoiceStudio voice against VoiceStudio's list.
    starter = OutboxStarter(info, temporal_client, load=lambda: settings)
    assert await starter.run_once() == Outcome.DISPATCHED
    handle = temporal_client.get_workflow_handle(workflow_id("7001", 2))
    description = await handle.describe()
    assert description.workflow_type == "VbeeDubbingWorkflow"
    assert row(conn, "7001")["workflow_id"] == "video-7001-g2"
    got = await started_input(temporal_client, workflow_id("7001", 2))
    assert got["voice"] == {
        "provider": "vbee",
        "voice_id": VBEE_CONFIGURED_VOICE,
        "settings": {"speed": 1.0},
    }
    assert CANARY not in (await handle.fetch_history()).to_json()
    # Neither VoiceStudio nor Vbee (paid) was called to check anything.
    assert stub.requests == []


async def test_missing_vbee_settings_pause_dispatch_without_touching_the_row(
    db: tuple[str, psycopg.Connection], temporal_client: Client
) -> None:
    info, conn = db
    add_video(conn, "7001")
    settings = vbee_settings(vbee_voice_id=None)
    starter = OutboxStarter(info, temporal_client, load=lambda: settings)
    assert await starter.run_once() == Outcome.CONFIG_ERROR
    got = row(conn, "7001")
    assert (got["state"], got["dispatch_attempts"], got["waiting"]) == ("pending", 0, False)
    assert (await temporal_client.count_workflows()).count == 0


async def test_runs_of_both_pipelines_count_against_one_limit(
    db: tuple[str, psycopg.Connection], temporal_client: Client
) -> None:
    info, conn = db
    add_video(conn, "7001")
    add_video(conn, "7002")
    vbee = OutboxStarter(
        info,
        temporal_client,
        load=lambda: vbee_settings(max_open_workflows=1),
        voices=lambda s: None,
    )
    assert await vbee.run_once() == Outcome.DISPATCHED
    # Once Temporal's visibility shows the Vbee run (nothing serves it, so it stays open).
    for _ in range(100):
        running = await temporal_client.count_workflows(
            'WorkflowType="VbeeDubbingWorkflow" AND ExecutionStatus="Running"'
        )
        if running.count == 1:
            break
        await asyncio.sleep(0.1)
    assert running.count == 1
    # A starter of the other pipeline (a fresh process, which knows of no start of its own)
    # sees the limit full.
    studio = OutboxStarter(
        info,
        temporal_client,
        load=lambda: Settings(voice_id="voice-a", s3_bucket=BUCKET, max_open_workflows=1),
        voices=lambda s: None,
    )
    assert await studio.run_once() == Outcome.THROTTLED
    assert row(conn, "7002")["state"] == "pending"


def test_the_default_pipeline_is_voicestudio() -> None:
    assert Settings().localization_pipeline == "voicestudio"

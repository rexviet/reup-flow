"""Starts a workflow for every row of `localization_outbox`.

    python -m localization.starter.outbox_starter

The downloader writes an outbox row in the same transaction that marks a video
`done`; this process turns rows into workflows. Each row is handled on its own:
claimed in a short transaction (no lock is held across a network call), checked,
started, then settled. One bad row never blocks the rows behind it.
"""

import asyncio
import enum
import logging
import re
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from opentelemetry import trace
from psycopg.rows import dict_row
from pydantic import ValidationError
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from localization.config import ConfigError, Settings, load_settings, voice_ref_from_settings
from localization.contracts import LocalizationInput, VoiceRef
from localization.contracts.ids import validate_channel_id, validate_source_key
from localization.observability.tracing import init_tracing, tag_session
from localization.providers.voices import VoiceCatalog, VoiceUnavailable
from localization.temporal.client import connect

log = logging.getLogger(__name__)

WORKFLOW_TYPE = "VideoLocalizationWorkflow"
WORKFLOW_QUEUE = "workflows"
EXECUTION_TIMEOUT_DAYS = 14
# A claim hides the row from other starters for this long; if this process dies
# between claim and settle, the row comes back due after it.
CLAIM_MINUTES = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
IDLE_SLEEP_SECONDS = 2.0
THROTTLE_SLEEP_SECONDS = 5.0
CONFIG_ERROR_SLEEP_SECONDS = 30.0

_S3_URL_RE = re.compile(r"s3://(?P<bucket>[^/]+)/(?P<key>.+)")

CLAIM_SQL = """
UPDATE localization_outbox
   SET next_attempt_at = now() + make_interval(mins => %(minutes)s)
 WHERE aweme_id = (SELECT aweme_id FROM localization_outbox
                    WHERE state = 'pending' AND next_attempt_at <= now()
                    ORDER BY next_attempt_at
                    LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING aweme_id, generation, dispatch_attempts
"""

VIDEO_SQL = "SELECT storage_path, sec_user_id FROM videos WHERE aweme_id = %s"

# Every settle is conditional on the row still being the one that was claimed: an
# operator who bumped `generation` to run the video again must not be overwritten.
DISPATCHED_SQL = """
UPDATE localization_outbox
   SET state = 'dispatched', dispatched_at = now(), workflow_id = %s, last_error = NULL
 WHERE aweme_id = %s AND generation = %s AND state = 'pending'
"""
REJECTED_SQL = """
UPDATE localization_outbox SET state = 'rejected', last_error = %s
 WHERE aweme_id = %s AND generation = %s AND state = 'pending'
"""
FAILED_SQL = """
UPDATE localization_outbox
   SET dispatch_attempts = dispatch_attempts + 1, last_error = %(error)s,
       state = CASE WHEN dispatch_attempts + 1 >= %(max_attempts)s THEN 'dead' ELSE state END,
       next_attempt_at = now() + make_interval(secs => %(delay)s)
 WHERE aweme_id = %(aweme_id)s AND generation = %(generation)s AND state = 'pending'
"""


class Outcome(enum.Enum):
    IDLE = "idle"
    THROTTLED = "throttled"
    CONFIG_ERROR = "config_error"
    DISPATCHED = "dispatched"
    REJECTED = "rejected"
    FAILED = "failed"


def workflow_id(aweme_id: str, generation: int) -> str:
    return f"video-{aweme_id}-g{generation}"


def backoff_seconds(attempts_so_far: int) -> int:
    """Exponential in the number of failures so far (0 -> 30s, 1 -> 60s, ...)."""
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempts_so_far)


class Rejected(Exception):
    """The row can never become a valid input; retrying it would change nothing."""


@dataclass(frozen=True)
class Claimed:
    aweme_id: str
    generation: int
    dispatch_attempts: int


def source_key(storage_path: str | None, bucket: str) -> str:
    match = _S3_URL_RE.fullmatch(storage_path or "")
    if match is None:
        raise Rejected("storage_path is not an s3:// URL")
    if match["bucket"] != bucket:
        raise Rejected("storage_path is in a different bucket than S3_BUCKET")
    try:
        return validate_source_key(match["key"])
    except ValueError:
        raise Rejected("storage_path is not a source/ object key") from None


class OutboxStarter:
    def __init__(
        self,
        database_url: str,
        client: Client,
        *,
        load: Callable[[], Settings] = load_settings,
        voices: Callable[[Settings], VoiceCatalog | None] | None = None,
    ) -> None:
        self._database_url = database_url
        self._client = client
        self._load = load
        self._catalogs: dict[tuple[str | None, str | None], VoiceCatalog] = {}
        self._voices = voices or self._catalog_for

    def _catalog_for(self, settings: Settings) -> VoiceCatalog | None:
        if not settings.voicestudio_url:
            return None
        key = (settings.voicestudio_url, settings.voice_id)
        if key not in self._catalogs:
            self._catalogs[key] = VoiceCatalog.from_settings(settings)
        return self._catalogs[key]

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        )

    async def _open_workflows(self) -> int:
        count = await self._client.count_workflows(
            f'WorkflowType="{WORKFLOW_TYPE}" AND ExecutionStatus="Running"'
        )
        return count.count

    @staticmethod
    def _start_input(
        settings: Settings, voice: VoiceRef, claimed: Claimed, row: dict
    ) -> LocalizationInput:
        try:
            return LocalizationInput(
                aweme_id=claimed.aweme_id,
                channel_id=validate_channel_id(row["sec_user_id"]),
                generation=claimed.generation,
                video_key=source_key(row["storage_path"], settings.s3_bucket),
                target_lang=settings.target_lang,
                voice=voice,
            )
        except (ValueError, ValidationError):
            raise Rejected("the row does not form a valid workflow input") from None

    async def run_once(self) -> Outcome:
        if await self._open_workflows() >= self._load().max_open_workflows:
            return Outcome.THROTTLED
        # The voice and language are read now, for THIS start: changing them
        # affects workflows started afterwards and never one already running.
        settings = self._load()
        try:
            voice = voice_ref_from_settings(settings)
            catalog = self._voices(settings)
            if catalog is not None:
                await catalog.ensure_exists(voice.voice_id)
        except (ConfigError, VoiceUnavailable) as exc:
            log.error("not dispatching: %s", exc)
            return Outcome.CONFIG_ERROR

        async with await self._connect() as conn:
            row = await self._claim(conn)
            if row is None:
                return Outcome.IDLE
            claimed = Claimed(row["aweme_id"], row["generation"], row["dispatch_attempts"])
            return await self._dispatch(conn, settings, voice, claimed)

    @staticmethod
    async def _claim(conn: psycopg.AsyncConnection) -> dict | None:
        """Its own transaction, committed before any network call: no row lock is
        held while a workflow is being started."""
        async with conn.transaction():
            cursor = await conn.execute(CLAIM_SQL, {"minutes": CLAIM_MINUTES})
            return await cursor.fetchone()

    async def _dispatch(
        self, conn: psycopg.AsyncConnection, settings: Settings, voice: VoiceRef, claimed: Claimed
    ) -> Outcome:
        id_ = workflow_id(claimed.aweme_id, claimed.generation)
        try:
            video = await (await conn.execute(VIDEO_SQL, (claimed.aweme_id,))).fetchone()
            if video is None:
                raise Rejected("the video row does not exist")
            spec = self._start_input(settings, voice, claimed, video)
            await self._start(spec, id_)
        except Rejected as exc:
            await conn.execute(REJECTED_SQL, (str(exc), claimed.aweme_id, claimed.generation))
            log.warning("rejected %s: %s", id_, exc)
            return Outcome.REJECTED
        except Exception as exc:  # noqa: BLE001 - any start failure is retried with backoff
            await conn.execute(
                FAILED_SQL,
                {
                    "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "max_attempts": settings.outbox_max_attempts,
                    "delay": backoff_seconds(claimed.dispatch_attempts),
                    "aweme_id": claimed.aweme_id,
                    "generation": claimed.generation,
                },
            )
            log.warning("dispatch of %s failed: %s", id_, type(exc).__name__)
            return Outcome.FAILED
        await conn.execute(DISPATCHED_SQL, (id_, claimed.aweme_id, claimed.generation))
        log.info("dispatched %s", id_)
        return Outcome.DISPATCHED

    async def _start(self, spec: LocalizationInput, id_: str) -> None:
        tracer = trace.get_tracer("localization.starter")
        with tracer.start_as_current_span("localization.start"):
            tag_session(spec.aweme_id)
            try:
                await self._client.start_workflow(
                    WORKFLOW_TYPE,
                    spec,
                    id=id_,
                    task_queue=WORKFLOW_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    execution_timeout=timedelta(days=EXECUTION_TIMEOUT_DAYS),
                )
            except WorkflowAlreadyStartedError:
                # The id carries the generation, so the same id is the same
                # dispatch: a previous attempt got as far as starting it.
                return

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        pauses = {
            Outcome.IDLE: IDLE_SLEEP_SECONDS,
            Outcome.THROTTLED: THROTTLE_SLEEP_SECONDS,
            Outcome.CONFIG_ERROR: CONFIG_ERROR_SLEEP_SECONDS,
        }
        while not stop.is_set():
            try:
                outcome = await self.run_once()
            except (psycopg.Error, RPCError, OSError):
                log.exception("starter loop error")
                outcome = Outcome.IDLE
            pause = pauses.get(outcome)
            if pause:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=pause)
                except TimeoutError:
                    pass


async def serve() -> None:
    settings = load_settings()
    if settings.loc_database_url is None:
        raise ConfigError("LOC_DATABASE_URL is not set")
    init_tracing(settings, "localization-starter")
    client = await connect(settings.temporal_address, settings.temporal_namespace)
    starter = OutboxStarter(settings.loc_database_url.get_secret_value(), client)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await starter.run_forever(stop)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())

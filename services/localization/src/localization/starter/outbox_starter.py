"""Starts a workflow for every row of `localization_outbox`.

    python -m localization.starter.outbox_starter

The downloader writes an outbox row in the same transaction that marks a video
`done`; this process turns rows into workflows. Each row is handled on its own:
claimed in a short transaction (no lock is held across a network call), checked,
started, then settled. One bad row never blocks the rows behind it.
"""

import argparse
import asyncio
import enum
import logging
import re
import signal
import sys
import time
from collections import deque
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
from temporalio.service import RPCError, RPCStatusCode

from localization.config import ConfigError, Settings, load_settings, voice_ref_from_settings
from localization.contracts import LocalizationInput, VoiceRef
from localization.contracts.ids import validate_aweme_id, validate_channel_id, validate_source_key
from localization.contracts.vbee_flow import VBEE_CONFIGURED_VOICE
from localization.contracts.voice import VbeeSettings
from localization.observability.tracing import init_tracing, tag_session
from localization.providers.voices import VoiceCatalog, VoiceUnavailable
from localization.temporal.client import connect

log = logging.getLogger(__name__)

WORKFLOW_TYPE = "VideoLocalizationWorkflow"
# The workflow `LOCALIZATION_PIPELINE=vbee` starts instead.
VBEE_WORKFLOW_TYPE = "VbeeDubbingWorkflow"
WORKFLOW_TYPES = {"voicestudio": WORKFLOW_TYPE, "vbee": VBEE_WORKFLOW_TYPE}
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


RELEASED_SQL = """
UPDATE localization_outbox
   SET last_error = %(error)s, next_attempt_at = now() + make_interval(secs => %(delay)s)
 WHERE aweme_id = %(aweme_id)s AND generation = %(generation)s AND state = 'pending'
"""

# A start that fails like this says nothing about the row: Temporal is unreachable,
# overloaded or refusing this client, and every other row would fail the same way.
# Counting it would turn an outage into a queue of dead rows.
SYSTEMIC_STATUSES = frozenset(
    {
        RPCStatusCode.UNAVAILABLE,
        RPCStatusCode.DEADLINE_EXCEEDED,
        RPCStatusCode.RESOURCE_EXHAUSTED,
        RPCStatusCode.PERMISSION_DENIED,
        RPCStatusCode.UNAUTHENTICATED,
        RPCStatusCode.NOT_FOUND,  # the namespace
        RPCStatusCode.CANCELLED,
    }
)
# For this long after this process starts a workflow, the running count is at least
# what this process knows it has reached: visibility lags, and without this the
# limit is overshot right after a burst.
RECENT_START_SECONDS = 3.0


def is_systemic(exc: Exception) -> bool:
    if isinstance(exc, RPCError):
        return exc.status in SYSTEMIC_STATUSES
    return isinstance(exc, OSError)


class Outcome(enum.Enum):
    IDLE = "idle"
    THROTTLED = "throttled"
    CONFIG_ERROR = "config_error"
    SYSTEMIC = "systemic"
    DISPATCHED = "dispatched"
    REJECTED = "rejected"
    FAILED = "failed"


def workflow_id(aweme_id: str, generation: int) -> str:
    return f"video-{aweme_id}-g{generation}"


def backoff_seconds(attempts_so_far: int) -> int:
    """Exponential in the number of failures so far (0 -> 30s, 1 -> 60s, ...)."""
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempts_so_far)


def vbee_voice(settings: Settings) -> VoiceRef:
    """The voice of a Vbee run: a marker the activities replace with VBEE_VOICE_ID, so the
    cloned voice's id never enters a workflow's history. Checks that the Vbee settings the
    run needs are present, without calling Vbee (every call is paid for)."""
    missing = [
        name
        for name, value in (
            ("VBEE_APP_ID", settings.vbee_app_id),
            ("VBEE_API_TOKEN", settings.vbee_api_token),
            ("VBEE_VOICE_ID", settings.vbee_voice_id),
        )
        if value is None or not value.get_secret_value()
    ]
    if missing:
        raise ConfigError(f"LOCALIZATION_PIPELINE=vbee needs {', '.join(missing)}")
    return VoiceRef(provider="vbee", voice_id=VBEE_CONFIGURED_VOICE, settings=VbeeSettings())


def run_voice(settings: Settings) -> VoiceRef:
    """The voice every new workflow is started with, for the configured pipeline."""
    if settings.localization_pipeline == "vbee":
        return vbee_voice(settings)
    return voice_ref_from_settings(settings)


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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._recent: deque[tuple[float, int]] = deque()  # (when, open count it reached)
        self._open = 0
        self._database_url = database_url
        self._client = client
        self._load = load
        self._catalogs: dict[tuple[str | None, str | None], VoiceCatalog] = {}
        self._voices = voices or self._catalog_for

    def _catalog_for(self, settings: Settings) -> VoiceCatalog | None:
        """The voice list to check VOICE_ID against, or None when the provider has
        no list of ours to check. For VoiceStudio the list is required: without it
        the server would silently replace an unknown id with a preset."""
        if settings.voice_provider != "voicestudio":
            return None
        if not settings.voicestudio_url:
            raise ConfigError("VOICESTUDIO_URL must be set to validate VOICE_ID")
        key = (settings.voicestudio_url, settings.voice_id)
        if key not in self._catalogs:
            self._catalogs[key] = VoiceCatalog.from_settings(settings)
        return self._catalogs[key]

    async def _connect(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            self._database_url, autocommit=True, row_factory=dict_row
        )

    async def _open_workflows(self) -> int:
        # Both pipelines share the workers: their runs count against one limit.
        types = " OR ".join(f'WorkflowType="{t}"' for t in WORKFLOW_TYPES.values())
        count = await self._client.count_workflows(f'({types}) AND ExecutionStatus="Running"')
        now = self._clock()
        while self._recent and now - self._recent[0][0] > RECENT_START_SECONDS:
            self._recent.popleft()
        known = self._recent[-1][1] if self._recent else 0
        self._open = max(count.count, known)
        return self._open

    @staticmethod
    def _start_input(
        settings: Settings, voice: VoiceRef, claimed: Claimed, row: dict
    ) -> LocalizationInput:
        try:
            return LocalizationInput(
                delivery_mode=(
                    settings.localization_delivery_mode
                    if voice.provider == "voicestudio"
                    else "legacy_render"
                ),
                aweme_id=claimed.aweme_id,
                channel_id=validate_channel_id(row["sec_user_id"]),
                generation=claimed.generation,
                video_key=source_key(row["storage_path"], settings.s3_bucket),
                target_lang=settings.target_lang,
                voice=voice,
            )
        except (ValueError, ValidationError):
            raise Rejected("the row does not form a valid workflow input") from None

    async def start_canary(self, aweme_id: str, generation: int) -> LocalizationInput:
        """Start exactly one new review generation without claiming/changing the outbox."""
        try:
            validate_aweme_id(aweme_id)
        except ValueError as exc:
            raise Rejected(str(exc)) from None
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 0 <= generation <= 1_000_000
        ):
            raise Rejected("generation must be an integer from 0 to 1000000")

        settings = self._load()
        if settings.localization_pipeline != "voicestudio":
            raise ConfigError("review canary requires the VoiceStudio pipeline")
        settings = settings.model_copy(update={"localization_delivery_mode": "review_project"})
        voice = run_voice(settings)
        if voice.provider != "voicestudio":
            raise ConfigError("review canary requires a VoiceStudio voice")
        if await self._open_workflows() >= settings.max_open_workflows:
            raise ConfigError("workflow capacity reached; retry canary later")
        catalog = self._voices(settings)
        if catalog is not None:
            await catalog.ensure_exists(voice.voice_id)
        async with await self._connect() as conn:
            video = await (await conn.execute(VIDEO_SQL, (aweme_id,))).fetchone()
            if video is None:
                raise Rejected("the video row does not exist")
            spec = self._start_input(settings, voice, Claimed(aweme_id, generation, 0), video)
            cursor = await conn.execute(
                "SELECT EXISTS (SELECT FROM localization_outbox WHERE aweme_id = %s "
                "AND generation >= %s UNION ALL SELECT FROM localization.runs "
                "WHERE aweme_id = %s AND generation >= %s) AS used",
                (aweme_id, generation, aweme_id, generation),
            )
            used = await cursor.fetchone()
            if used["used"]:
                raise Rejected("canary generation must be newer than all recorded generations")
            # REJECT_DUPLICATE covers races/Temporal histories missing from the DB.
            # Unlike outbox retry, an existing canary is an operator error.
            await self._start(spec, workflow_id(aweme_id, generation), ignore_duplicate=False)
        return spec

    async def run_once(self) -> Outcome:
        if await self._open_workflows() >= self._load().max_open_workflows:
            return Outcome.THROTTLED
        # The voice and language are read now, for THIS start: changing them
        # affects workflows started afterwards and never one already running.
        settings = self._load()
        try:
            voice = run_voice(settings)
            # A Vbee run never asks VoiceStudio: its voice is not one of VoiceStudio's.
            catalog = None if voice.provider == "vbee" else self._voices(settings)
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
            await self._start(spec, id_, WORKFLOW_TYPES[settings.localization_pipeline])
        except Rejected as exc:
            await conn.execute(REJECTED_SQL, (str(exc), claimed.aweme_id, claimed.generation))
            log.warning("rejected %s: %s", id_, exc)
            return Outcome.REJECTED
        except Exception as exc:  # noqa: BLE001 - any start failure is retried with backoff
            if is_systemic(exc):
                await conn.execute(
                    RELEASED_SQL,
                    {
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                        "delay": CONFIG_ERROR_SLEEP_SECONDS,
                        "aweme_id": claimed.aweme_id,
                        "generation": claimed.generation,
                    },
                )
                log.warning("start of %s failed systemically: %s", id_, type(exc).__name__)
                return Outcome.SYSTEMIC
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
        self._recent.append((self._clock(), self._open + 1))
        await conn.execute(DISPATCHED_SQL, (id_, claimed.aweme_id, claimed.generation))
        log.info("dispatched %s", id_)
        return Outcome.DISPATCHED

    async def _start(
        self,
        spec: LocalizationInput,
        id_: str,
        workflow_type: str = WORKFLOW_TYPE,
        *,
        ignore_duplicate: bool = True,
    ) -> None:
        tracer = trace.get_tracer("localization.starter")
        with tracer.start_as_current_span("localization.start"):
            tag_session(spec.aweme_id)
            try:
                await self._client.start_workflow(
                    workflow_type,
                    spec,
                    id=id_,
                    task_queue=WORKFLOW_QUEUE,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    execution_timeout=timedelta(days=EXECUTION_TIMEOUT_DAYS),
                )
            except WorkflowAlreadyStartedError:
                if not ignore_duplicate:
                    raise
                # The id carries the generation, so the same id is the same
                # dispatch: a previous attempt got as far as starting it.
                return

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        pauses = {
            Outcome.IDLE: IDLE_SLEEP_SECONDS,
            Outcome.THROTTLED: THROTTLE_SLEEP_SECONDS,
            Outcome.CONFIG_ERROR: CONFIG_ERROR_SLEEP_SECONDS,
            Outcome.SYSTEMIC: CONFIG_ERROR_SLEEP_SECONDS,
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


async def serve(canary_identity: tuple[str, int] | None = None) -> None:
    settings = load_settings()
    if settings.loc_database_url is None:
        raise ConfigError("LOC_DATABASE_URL is not set")
    init_tracing(settings, "localization-starter")
    client = await connect(settings.temporal_address, settings.temporal_namespace)
    starter = OutboxStarter(settings.loc_database_url.get_secret_value(), client)
    if canary_identity is not None:
        spec = await starter.start_canary(*canary_identity)
        log.info(
            "started canary %s with delivery_mode=%s",
            workflow_id(*canary_identity),
            spec.delivery_mode,
        )
        return
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await starter.run_forever(stop)


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    canary = commands.add_parser(
        "canary", help="start one new review generation; no outbox mutation"
    )
    canary.add_argument("aweme_id")
    canary.add_argument("generation", type=int)
    args = parser.parse_args()
    canary_identity = (args.aweme_id, args.generation) if args.command == "canary" else None
    asyncio.run(serve(canary_identity))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""The `localization.llm_calls` table: one row per model request, keyed by the
provider's generation id."""

import asyncio
import logging

import psycopg

from localization.observability.llm_usage import LlmUsage, RunContext

log = logging.getLogger(__name__)

# A database that does not answer must not hold a model request back for long.
WRITE_TIMEOUT_SECONDS = 5.0

INSERT_SQL = """
INSERT INTO localization.llm_calls
  (generation_id, aweme_id, generation, stage, model, prompt_tokens, completion_tokens, cost_usd)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (generation_id) DO NOTHING
"""


class LlmCallRecorder:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def __call__(self, usage: LlmUsage, run: RunContext) -> None:
        """Best effort: a failed write is logged, never raised, so it cannot fail the
        model request it is about. The log names the error type only (a connection
        error can carry the connection string)."""
        try:
            await asyncio.wait_for(self._write(usage, run), WRITE_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record llm call %s: %s", usage.generation_id, type(exc).__name__)

    async def _write(self, usage: LlmUsage, run: RunContext) -> None:
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            await conn.execute(
                INSERT_SQL,
                (
                    usage.generation_id,
                    run.aweme_id,
                    run.generation,
                    run.stage,
                    usage.model,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    usage.cost_usd,
                ),
            )

"""Step 9: how a run ends.

A run that passed review is copied (inside the bucket, no bytes through this process)
to `output/{aweme_id}.mp4`. A run that needs a person keeps its render under
`review/{aweme_id}-g{generation}.mp4`, which no lifecycle expires, and is never
written to `output/`. A run that failed has no render at all. Each of the three also
leaves one row in `localization.runs`, keyed by video and generation, so the same
call made twice leaves the same state.
"""

import asyncio
import json

import psycopg
from psycopg.types.json import Jsonb
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.contracts import LocalizationInput, PublishRequest, PublishResult
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore, output_key, review_key

UPSERT_SQL = """
INSERT INTO localization.runs
  (aweme_id, generation, workflow_id, status, output_key, review_key, verdict,
   review_rounds, needs_human, llm_cost_usd, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
        (SELECT SUM(cost_usd) FROM localization.llm_calls WHERE aweme_id = %s AND generation = %s),
        now())
ON CONFLICT (aweme_id, generation) DO UPDATE SET
  workflow_id = EXCLUDED.workflow_id,
  status = EXCLUDED.status,
  output_key = EXCLUDED.output_key,
  review_key = EXCLUDED.review_key,
  verdict = EXCLUDED.verdict,
  review_rounds = EXCLUDED.review_rounds,
  needs_human = EXCLUDED.needs_human,
  llm_cost_usd = EXCLUDED.llm_cost_usd,
  updated_at = now()
"""


class RunStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def upsert(
        self,
        *,
        aweme_id: str,
        generation: int,
        workflow_id: str,
        status: str,
        output: str | None,
        review: str | None,
        record: dict,
        review_rounds: int,
        needs_human: bool,
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            await conn.execute(
                UPSERT_SQL,
                (
                    aweme_id,
                    generation,
                    workflow_id,
                    status,
                    output,
                    review,
                    Jsonb(record),
                    review_rounds,
                    needs_human,
                    aweme_id,
                    generation,
                ),
            )


def _refuse(message: str) -> ApplicationError:
    return ApplicationError(message, type="PublishRefused", non_retryable=True)


class PublishActivity:
    def __init__(self, store: ObjectStore, runs: RunStore) -> None:
        self._store = store
        self._runs = runs

    @activity.defn(name="publish")
    async def publish(self, spec: LocalizationInput, request: PublishRequest) -> PublishResult:
        tag_session(spec.aweme_id)
        output: str | None = None
        review: str | None = None
        if request.outcome == "failed":
            if request.render_key is not None:
                raise _refuse("a failed run has no render to keep")
        else:
            if request.render_key is None:
                raise _refuse(f"a {request.outcome} run needs the render it is about")
            if request.outcome == "published":
                # `output/` is what the world sees: only a render the Reviewer passed goes
                # there, whatever the caller meant.
                if request.verdict is None or not request.verdict.passed or request.reasons:
                    raise _refuse(
                        "only a render that passed review, with nothing flagged, is published"
                    )
                output = output_key(spec.aweme_id)
                await asyncio.to_thread(
                    self._store.copy, request.render_key, output, produced_by="render"
                )
            else:
                review = review_key(spec.aweme_id, spec.generation)
                await asyncio.to_thread(
                    self._store.copy, request.render_key, review, produced_by="render"
                )
        record = {
            "reasons": list(request.reasons),
            "verdict": (
                None
                if request.verdict is None
                else json.loads(request.verdict.model_dump_json(by_alias=True))
            ),
        }
        await self._runs.upsert(
            aweme_id=spec.aweme_id,
            generation=spec.generation,
            workflow_id=activity.info().workflow_id,
            status=request.outcome,
            output=output,
            review=review,
            record=record,
            review_rounds=request.review_rounds,
            needs_human=request.outcome == "needs_human",
        )
        return PublishResult(outcome=request.outcome, output_key=output, review_key=review)

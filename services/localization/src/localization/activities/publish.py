"""Step 9: how a run ends.

A run that passed review is copied (inside the bucket, no bytes through this process)
to `output/{aweme_id}.mp4`. A run that needs a person keeps its render under
`review/{aweme_id}-g{generation}.mp4`, which no lifecycle expires, and is never
written to `output/`. A run that failed has no render at all, and neither has one that
skipped a product advertisement (`skipped_ad`: nothing past the preparation ran). A video
without a voice (`published_original`) has no render either: its source is copied, the
same way, to `output/{aweme_id}.mp4` as it is. A file without a video stream
(`skipped_no_video`) is skipped before anything is prepared and has nothing at all. Each of
the six also leaves one row in
`localization.runs`, keyed by video and generation, so the same call made twice leaves the
same state.

Last, the run's scores go on its Langfuse trace (see `observability.scores`), best
effort: a run is published, kept or failed whatever happens to its scores. A skipped
video has none, and neither has a published original: nothing of either was made, so
there is no quality to measure.
"""

import asyncio
import json
import logging

import psycopg
from psycopg.types.json import Jsonb
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.review_project import resolve_project
from localization.activities.script_document import read_script
from localization.contracts import (
    DubSegmentsDocument,
    LocalizationInput,
    LocalizedScript,
    PublishRequest,
    PublishResult,
)
from localization.observability.scores import LangfuseScores, current_trace_id, run_scores
from localization.observability.tracing import tag_session
from localization.storage.object_store import ObjectStore, output_key, project_head_key, review_key

log = logging.getLogger(__name__)

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


UPSERT_PROJECT_SQL = (
    UPSERT_SQL.replace("review_key, verdict,", "review_key, project_key, verdict,")
    .replace(
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,",
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,",
    )
    .replace(
        "  verdict = EXCLUDED.verdict,",
        "  project_key = EXCLUDED.project_key,\n  verdict = EXCLUDED.verdict,",
    )
)


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
        project: str | None = None,
    ) -> None:
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            await conn.execute(
                (UPSERT_SQL if project is None else UPSERT_PROJECT_SQL),
                (
                    aweme_id,
                    generation,
                    workflow_id,
                    status,
                    output,
                    review,
                    *((project,) if project is not None else ()),
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
    def __init__(
        self, store: ObjectStore, runs: RunStore, scores: LangfuseScores | None = None
    ) -> None:
        self._store = store
        self._runs = runs
        self._scores = scores

    @activity.defn(name="publish")
    async def publish(self, spec: LocalizationInput, request: PublishRequest) -> PublishResult:
        tag_session(spec.aweme_id)
        output: str | None = None
        review: str | None = None
        if request.project_key is not None:
            if (
                spec.delivery_mode != "review_project"
                or spec.voice.provider != "voicestudio"
                or request.outcome not in ("ready_for_review", "needs_human")
                or request.render_key is not None
                or request.project_key != project_head_key(spec.aweme_id, spec.generation)
            ):
                raise _refuse("invalid review project publication")
            if request.outcome == "ready_for_review" and (
                request.verdict is None
                or not request.verdict.passed
                or request.reasons
                or request.safety_flag
            ):
                raise _refuse("ready_for_review requires a clean data review")
            _, manifest, _ = await asyncio.to_thread(
                resolve_project, self._store, request.project_key
            )
            if manifest.source != spec.source_snapshot or manifest.voice != spec.voice:
                raise _refuse("project provenance differs from workflow input")
            if request.outcome == "ready_for_review" and (
                any(c.status != "translated" or c.overflow_unresolved for c in manifest.cues)
                or request.verdict.issues
            ):
                raise _refuse("flagged project requires human review")
        elif request.outcome == "ready_for_review":
            raise _refuse("ready_for_review needs a durable project HEAD")
        elif request.outcome in ("failed", "skipped_ad", "skipped_no_video", "published_original"):
            if request.render_key is not None:
                raise _refuse(f"a {request.outcome} run has no render to keep")
            if request.outcome == "published_original":
                # Nothing was made, so nothing was reviewed: what goes out is the source the
                # run was started on, untouched.
                if request.verdict is not None or request.reasons or request.dub is not None:
                    raise _refuse("an original is published only for a run that made nothing")
                output = output_key(spec.aweme_id)
                await asyncio.to_thread(
                    self._store.copy, spec.video_key, output, produced_by="download"
                )
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
            **({"project": request.project_key} if request.project_key is not None else {}),
        )
        try:
            await self._score(request)
        except Exception as exc:  # noqa: BLE001 - a run is never failed for its scores
            log.warning("run scores not sent: %s", type(exc).__name__)
        return PublishResult(
            outcome=request.outcome,
            output_key=output,
            review_key=review,
            project_key=request.project_key,
        )

    async def _score(self, request: PublishRequest) -> None:
        trace_id = current_trace_id()
        if self._scores is None or trace_id is None:
            return
        if request.outcome in ("skipped_ad", "skipped_no_video", "published_original"):
            # Its review rounds and its `needs_human` would read as a video that went out
            # unreviewed; a video that was never localized is not one to score.
            return
        segments: DubSegmentsDocument | None = None
        script: LocalizedScript | None = None
        if request.dub is not None:
            try:
                segments, script = await asyncio.to_thread(self._dub_documents, request)
            except Exception as exc:  # noqa: BLE001 - a missing document costs its scores only
                log.warning("run scores without the dub documents: %s", type(exc).__name__)
        scores = run_scores(
            verdict=request.verdict,
            segments=segments,
            script=script,
            review_rounds=request.review_rounds,
            needs_human=request.outcome == "needs_human",
            safety_flag=request.safety_flag,
        )
        await self._scores.send(trace_id, scores)

    def _dub_documents(
        self, request: PublishRequest
    ) -> tuple[DubSegmentsDocument, LocalizedScript]:
        assert request.dub is not None
        segments = DubSegmentsDocument.model_validate_json(
            self._store.get_bytes(request.dub.segments_key, produced_by="dub_script")
        )
        return segments, read_script(self._store, request.dub.script_key)

"""G8: what changes if the preparation stages 2a (transcribe) and 2b (detect_subtitles)
become two tools that a model runs from CodeMode, instead of two activities the workflow
starts itself (`VideoLocalizationWorkflow._prepare`)?

Measured, for the same two activities, in both shapes:
  M1  Time from workflow start to the first stage starting, and total wall time.
      With CodeMode a model request comes first; `code` uses a scripted model (framework
      overhead only), `real:<model>` uses a real model through OpenRouter.
  M2  One stage fails while the other is still running. Does the failure reach the run at
      once, and is the sibling cancelled (the workflow cancels it today)?
  M3  What reaches the workflow and the model. The workflow needs the whole transcript
      for later stages; does it get it, and what does the model see?

Run (inside the compose network):
  ./run.sh uv run python g8_prep_code_mode.py <direct|code|real:MODEL> <happy|fail>
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import timedelta

import psycopg
from pydantic_ai import Agent, RunContext
from pydantic_ai.durable_exec.temporal import (
    AgentPlugin,
    PydanticAIPlugin,
    PydanticAIWorkflow,
    TemporalDurability,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness import CodeMode
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig

MODE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "worker" else (sys.argv[2] if len(sys.argv) > 2 else "code")
SCENARIO = next((a for a in sys.argv[1:] if a in ("happy", "fail")), "happy")
REAL = MODE.split(":", 1)[1] if MODE.startswith("real:") else None
ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
PGUSER = os.environ.get("POSTGRES_USER", "reup")
PGPASSWORD = os.environ.get("POSTGRES_PASSWORD", "reup")
DB = "spike_g8"
DB_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@postgres:5432/{DB}"
ADMIN_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@postgres:5432/postgres"

MODEL_QUEUE, IO_QUEUE, WORKFLOW_QUEUE = "g8-llm", "g8-io", "g8-workflows"

# The two stages. `fail` makes 2b fail early while 2a is still far from done.
STT_SECONDS = 6.0 if SCENARIO == "fail" else 3.0
DETECT_SECONDS = 2.0
DETECT_FAIL_AFTER = 0.5 if SCENARIO == "fail" else None
TRANSCRIPT_CHARS = 200_000  # a transcript far larger than a real one, so size effects show

CODE = """\
import asyncio
t, d = await asyncio.gather(transcribe(), detect_subtitles())
{"transcript_chars": len(t), "subtitles": d}
"""

_cfg = ActivityConfig(
    task_queue=IO_QUEUE,
    start_to_close_timeout=timedelta(seconds=60),
    heartbeat_timeout=timedelta(seconds=5),
    retry_policy=RetryPolicy(maximum_attempts=1),
)
_model_cfg = ActivityConfig(task_queue=MODEL_QUEUE, start_to_close_timeout=timedelta(seconds=120))


# --------------------------------------------------------------------------- the two stages


async def _event(c, run_id: str, name: str, outcome: str | None = None, *, end: bool = False) -> None:
    if end:
        await c.execute(
            "UPDATE events SET t_end=clock_timestamp(), outcome=%s WHERE run_id=%s AND name=%s",
            (outcome, run_id, name),
        )
    else:
        await c.execute(
            "INSERT INTO events(run_id, name, t_start) VALUES (%s,%s,clock_timestamp())", (run_id, name)
        )


async def _stage(run_id: str, name: str, seconds: float, fail_after: float | None, payload: str) -> str:
    """One stage: heartbeats while it works, so a cancel reaches it, and records how it ended."""
    async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as c:
        await _event(c, run_id, name)
        began = time.monotonic()
        try:
            while time.monotonic() - began < seconds:
                if fail_after is not None and time.monotonic() - began >= fail_after:
                    raise RuntimeError(f"{name} failed")
                activity.heartbeat()
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            await _event(c, run_id, name, "cancelled", end=True)
            raise
        except RuntimeError:
            await _event(c, run_id, name, "failed", end=True)
            raise
        await _event(c, run_id, name, "done", end=True)
    return payload


# What the workflow starts today: two activities.
@activity.defn(name="g8_transcribe")
async def g8_transcribe(run_id: str) -> str:
    return await _stage(run_id, "transcribe", STT_SECONDS, None, "w" * TRANSCRIPT_CHARS)


@activity.defn(name="g8_detect")
async def g8_detect(run_id: str) -> str:
    return await _stage(run_id, "detect", DETECT_SECONDS, DETECT_FAIL_AFTER, "3 subtitle bands")


# The same two stages as tools of an agent.
async def transcribe(ctx: RunContext[str]) -> str:
    """Speech to text on the isolated vocals. Returns the transcript."""
    return await _stage(ctx.deps, "transcribe", STT_SECONDS, None, "w" * TRANSCRIPT_CHARS)


async def detect_subtitles(ctx: RunContext[str]) -> str:
    """Finds the burned-in subtitle band. Returns a short description."""
    return await _stage(ctx.deps, "detect", DETECT_SECONDS, DETECT_FAIL_AFTER, "3 subtitle bands")


async def _scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if sum(isinstance(m, ModelResponse) for m in messages) == 0:
        return ModelResponse(parts=[ToolCallPart("run_code", {"code": CODE})])
    # Whatever the model is shown after its code ran: this is the M2/M3 evidence.
    last = messages[-1]
    saw = [f"{type(p).__name__}: {str(getattr(p, 'content', ''))[:300]}" for p in last.parts]
    return ModelResponse(parts=[TextPart(json.dumps({"model_saw": saw}))])


def _model():
    if not REAL:
        return FunctionModel(_scripted_model)
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(
        REAL,
        provider=OpenAIProvider(
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=os.environ["OPENROUTER_API_KEY"],
        ),
    )


agent: Agent[str, str] = Agent(
    _model(),
    name="g8",
    deps_type=str,
    instructions=(
        "Run the preparation of a video. Call transcribe and detect_subtitles together, in ONE run_code "
        "snippet, with asyncio.gather so they run in parallel. End the snippet with the expression "
        '{"transcript_chars": len(t), "subtitles": d}. Then reply with that value as JSON.'
    ),
    capabilities=[
        CodeMode(),
        TemporalDurability(
            activity_config=ActivityConfig(start_to_close_timeout=timedelta(seconds=60)),
            model_activity_config=_model_cfg,
        ),
    ],
)
agent.tool(transcribe, name="transcribe", metadata={"temporal": _cfg})
agent.tool(detect_subtitles, name="detect_subtitles", metadata={"temporal": _cfg})


# --------------------------------------------------------------------------- the workflows


@workflow.defn(name="G8Direct")
class G8Direct:
    """The shape of `_prepare` today: start both, wait for the first failure, cancel the rest."""

    @workflow.run
    async def run(self, run_id: str) -> str:
        a = workflow.start_activity("g8_transcribe", run_id, result_type=str, **_direct_kwargs())
        b = workflow.start_activity("g8_detect", run_id, result_type=str, **_direct_kwargs())
        try:
            await workflow.wait([a, b], return_when=asyncio.FIRST_EXCEPTION)
            # As `_prepare` does: a stage that already failed is settled first.
            done = {}
            for name, h in sorted([("a", a), ("b", b)], key=lambda p: not (p[1].done() and p[1].exception())):
                done[name] = await h
            transcript, subtitles = done["a"], done["b"]
        except BaseException:
            for h in (a, b):
                if not h.done():
                    h.cancel()
            raise
        return json.dumps({"largest_value_chars_the_workflow_holds": len(transcript), "subtitles": subtitles})


def _direct_kwargs() -> dict:
    return {
        "task_queue": IO_QUEUE,
        "start_to_close_timeout": timedelta(seconds=60),
        "heartbeat_timeout": timedelta(seconds=5),
        "retry_policy": RetryPolicy(maximum_attempts=1),
    }


@workflow.defn(name="G8Code")
class G8Code(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [agent]

    @workflow.run
    async def run(self, run_id: str) -> str:
        result = await agent.run(f"Prepare the video of run {run_id}.", deps=run_id)
        largest = max(
            (len(str(getattr(p, "content", ""))) for m in result.all_messages() for p in m.parts), default=0
        )
        return json.dumps({"largest_message_part_chars_in_workflow": largest, "output": str(result.output)[:300]})


# --------------------------------------------------------------------------- worker


async def worker_main() -> None:
    mod = __import__("g8_prep_code_mode")
    client = await Client.connect(ADDRESS, namespace=NAMESPACE, plugins=[PydanticAIPlugin()])
    workers = [
        Worker(client, task_queue=WORKFLOW_QUEUE, workflows=[mod.G8Direct, mod.G8Code]),
        Worker(client, task_queue=IO_QUEUE, activities=[mod.g8_transcribe, mod.g8_detect], plugins=[AgentPlugin(mod.agent)]),
        Worker(client, task_queue=MODEL_QUEUE, plugins=[AgentPlugin(mod.agent)]),
    ]
    await asyncio.gather(*(w.run() for w in workers))


# --------------------------------------------------------------------------- orchestrator


def _setup_db() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(
            "CREATE TABLE events(n serial PRIMARY KEY, run_id text, name text,"
            " t_start timestamptz, t_end timestamptz, outcome text)"
        )


async def orchestrate() -> int:
    _setup_db()
    client = await Client.connect(ADDRESS, namespace=NAMESPACE, plugins=[PydanticAIPlugin()])
    run_id = f"g8-{MODE.replace(':', '-').replace('/', '-')}-{SCENARIO}-{int(time.time())}"
    worker = subprocess.Popen([sys.executable, __file__, "worker", MODE, SCENARIO])
    await asyncio.sleep(5)

    async def mark(name: str) -> None:
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as c:
            await _event(c, run_id, name)

    await mark("workflow_start")
    handle = await client.start_workflow(
        "G8Direct" if MODE == "direct" else "G8Code", run_id, id=run_id, task_queue=WORKFLOW_QUEUE
    )
    try:
        output = await asyncio.wait_for(handle.result(), timeout=240)
    except Exception as e:  # noqa: BLE001 - a spike records the failure instead of hiding it
        cause = repr(getattr(e, "cause", None) or e.__cause__)[:400]
        output = f"WORKFLOW FAILED: {type(e).__name__}: {str(e)[:200]} | cause: {cause}"
    await mark("workflow_end")
    await asyncio.sleep(8)  # long enough for a cancelled or orphaned sibling to finish or be recorded
    worker.terminate()
    worker.wait()

    with psycopg.connect(DB_URL) as c:
        ev = {
            r[0]: r[1:]
            for r in c.execute(
                "SELECT name, EXTRACT(EPOCH FROM t_start), EXTRACT(EPOCH FROM t_end), outcome FROM events"
            ).fetchall()
        }
    t0 = float(ev["workflow_start"][0])
    rel = lambda v: None if v is None else round(float(v) - t0, 2)  # noqa: E731
    stages = {n: {"start": rel(ev[n][0]), "end": rel(ev[n][1]), "outcome": ev[n][2]} for n in ("transcribe", "detect") if n in ev}
    starts = [s["start"] for s in stages.values()]
    history_bytes = 0
    n_model, n_tool = 0, 0
    async for e in handle.fetch_history_events():
        history_bytes += e.ByteSize()
        if e.HasField("activity_task_scheduled_event_attributes"):
            kind = e.activity_task_scheduled_event_attributes.activity_type.name
            n_model += "model_request" in kind
            n_tool += "call_tool" in kind or kind.startswith("g8_")
    summary = {
        "mode": MODE,
        "scenario": SCENARIO,
        "first_stage_starts_after_s": min(starts) if starts else None,
        "workflow_finished_after_s": rel(ev["workflow_end"][0]),
        "stages": stages,
        "model_request_activities": n_model,
        "tool_or_stage_activities": n_tool,
        "history_bytes": history_bytes,
        "workflow_output": output,
    }
    os.makedirs("out", exist_ok=True)
    with open(f"out/g8-{MODE.replace(':', '-').replace('/', '-')}-{SCENARIO}.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    if "worker" in sys.argv[1:2]:
        asyncio.run(worker_main())
    else:
        sys.exit(asyncio.run(orchestrate()))

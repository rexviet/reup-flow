"""G1: does a pydantic-ai durable agent resume after a worker SIGKILL, without
re-running completed work, with the tool pinned to its own task queue?

Run (inside the compose network):  ./run.sh uv run python g1_durable_agent.py [--api durability|temporal-agent]

Model leg: a deterministic FunctionModel, so the durability property is
measured without a provider. If OPENROUTER_API_KEY is set, a second leg asks
each model in G1_OPENROUTER_MODELS whether it can call the tool at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import warnings
from datetime import timedelta

import psycopg
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import (
    PydanticAIPlugin,
    AgentPlugin,
    PydanticAIWorkflow,
    TemporalAgent,
    TemporalDurability,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig

API = "temporal-agent" if "--api" in sys.argv and sys.argv[sys.argv.index("--api") + 1] == "temporal-agent" else "durability"
REAL = sys.argv[sys.argv.index("--real") + 1] if "--real" in sys.argv else None
ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
PGUSER = os.environ.get("POSTGRES_USER", "reup")
PGPASSWORD = os.environ.get("POSTGRES_PASSWORD", "reup")
DB = "spike_g1"
DB_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@postgres:5432/{DB}"
ADMIN_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@postgres:5432/postgres"

MODEL_QUEUE, TOOL_QUEUE, WORKFLOW_QUEUE = "llm", "io", "workflows"
MODEL_SLOW_TURN_SECONDS = 4  # the window in which the worker is killed


def _run_id_from(messages: list[ModelMessage]) -> str:
    for m in messages:
        for p in getattr(m, "parts", []):
            if isinstance(p, UserPromptPart) and isinstance(p.content, str) and "run_id=" in p.content:
                return p.content.split("run_id=", 1)[1].split()[0]
    raise RuntimeError("run_id not found in prompt")


async def _scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Turn 1 calls the tool with call_no=1, turn 2 with call_no=2, turn 3 answers.

    Every invocation is recorded in model_calls with the pid, so a model request
    that ran twice across the kill is visible rather than inferred.
    """
    run_id = _run_id_from(messages)
    turn = 1 + sum(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", []))
    async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as c:
        await c.execute("INSERT INTO model_calls(run_id, turn, pid) VALUES (%s,%s,%s)", (run_id, turn, os.getpid()))
    if turn == 2:
        await asyncio.sleep(MODEL_SLOW_TURN_SECONDS)
    if turn <= 2:
        return ModelResponse(parts=[ToolCallPart("count_call", {"run_id": run_id, "call_no": turn})])
    return ModelResponse(parts=[TextPart("done")])


async def count_call(run_id: str, call_no: int) -> str:
    """UPSERT one row per (run_id, call_no); `hits` exposes any re-execution."""
    async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as c:
        await c.execute(
            "INSERT INTO spike_calls(run_id, call_no, hits) VALUES (%s,%s,1) "
            "ON CONFLICT (run_id, call_no) DO UPDATE SET hits = spike_calls.hits + 1",
            (run_id, call_no),
        )
    return f"recorded {call_no}"


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


_tool_cfg = ActivityConfig(task_queue=TOOL_QUEUE, start_to_close_timeout=timedelta(seconds=30))
_model_cfg = ActivityConfig(task_queue=MODEL_QUEUE, start_to_close_timeout=timedelta(seconds=30))
_base_cfg = ActivityConfig(start_to_close_timeout=timedelta(seconds=30))

if API == "durability":
    agent = Agent(
        _model(),
        name="g1",
        tools=[],
        capabilities=[TemporalDurability(activity_config=_base_cfg, model_activity_config=_model_cfg)],
    )
    agent.tool_plain(count_call, metadata={"temporal": _tool_cfg})
    durable = agent
else:
    warnings.simplefilter("ignore")
    inner = Agent(_model(), name="g1", tools=[])
    inner.tool_plain(count_call)
    durable = TemporalAgent(
        inner,
        activity_config=_base_cfg,
        model_activity_config=_model_cfg,
        tool_activity_config={"<agent>": {"count_call": _tool_cfg}},
    )


@workflow.defn(name="G1Workflow")
class G1Workflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [durable]

    @workflow.run
    async def run(self, run_id: str) -> str:
        prompt = f"run_id={run_id}"
        if REAL:
            prompt += (
                ". Call the count_call tool with this run_id and call_no=1, wait for its result, "
                "then call it again with call_no=2, then reply with the single word done."
            )
        result = await durable.run(prompt)
        return result.output


# --------------------------------------------------------------------------- worker


async def worker_main() -> None:
    mod = __import__("g1_durable_agent")
    client = await Client.connect(ADDRESS, namespace=NAMESPACE, plugins=[PydanticAIPlugin()])
    workers = [Worker(client, task_queue=WORKFLOW_QUEUE, workflows=[mod.G1Workflow])]
    # PydanticAIPlugin registers the agent's activities only on the worker that
    # hosts the workflow. The model and tool queues need AgentPlugin to register
    # them there, and one worker per queue is what makes the pinning observable.
    workers += [
        Worker(client, task_queue=q, plugins=[AgentPlugin(mod.durable)]) for q in (MODEL_QUEUE, TOOL_QUEUE)
    ]
    await asyncio.gather(*(w.run() for w in workers))


# --------------------------------------------------------------------------- orchestrator


def _setup_db() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("CREATE TABLE spike_calls(run_id text, call_no int, hits int, PRIMARY KEY (run_id, call_no))")
        c.execute("CREATE TABLE model_calls(run_id text, turn int, pid int, at timestamptz DEFAULT now())")


def _spawn_worker() -> subprocess.Popen:
    extra = ["--real", REAL] if REAL else []
    return subprocess.Popen([sys.executable, __file__, "worker", "--api", API, *extra])


async def _activities(handle) -> list[dict]:
    """Scheduled activities in history order with their task queue and outcome."""
    from temporalio.api.enums.v1 import EventType

    events = [e async for e in handle.fetch_history_events()]
    scheduled: dict[int, dict] = {}
    for e in events:
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            a = e.activity_task_scheduled_event_attributes
            scheduled[e.event_id] = {"event_id": e.event_id, "type": a.activity_type.name, "queue": a.task_queue.name, "done": False}
        elif e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            sid = e.activity_task_completed_event_attributes.scheduled_event_id
            if sid in scheduled:
                scheduled[sid]["done"] = True
    return list(scheduled.values())


async def orchestrate() -> int:
    _setup_db()
    client = await Client.connect(ADDRESS, namespace=NAMESPACE, plugins=[PydanticAIPlugin()])
    run_id = f"g1-{int(time.time())}"
    worker = _spawn_worker()
    await asyncio.sleep(4)
    handle = await client.start_workflow("G1Workflow", run_id, id=run_id, task_queue=WORKFLOW_QUEUE)

    killed_after = None
    deadline = time.time() + 60
    while time.time() < deadline and killed_after is None:
        for a in await _activities(handle):
            if a["done"] and "call_tool" in a["type"]:
                worker.send_signal(signal.SIGKILL)
                killed_after = a
                break
        await asyncio.sleep(0.05)
    if killed_after is None:
        print("FAIL: never saw a completed tool activity to kill after")
        worker.kill()
        return 1
    worker.wait()
    snapshot = await _activities(handle)
    time.sleep(1)
    worker = _spawn_worker()
    output = await asyncio.wait_for(handle.result(), timeout=120)
    worker.send_signal(signal.SIGTERM)
    worker.wait()

    acts = await _activities(handle)
    with psycopg.connect(DB_URL) as c:
        calls = c.execute("SELECT call_no, hits FROM spike_calls WHERE run_id=%s ORDER BY call_no", (run_id,)).fetchall()
        models = c.execute("SELECT turn, count(*) FROM model_calls WHERE run_id=%s GROUP BY turn ORDER BY turn", (run_id,)).fetchall()
        pids = dict(c.execute("SELECT turn, min(pid) FROM model_calls WHERE run_id=%s GROUP BY turn", (run_id,)).fetchall())
    tool_acts = [a for a in acts if "call_tool" in a["type"]]
    model_acts = [a for a in acts if "model_request" in a["type"]]

    if REAL:
        # A real model is not scripted: assert what must hold whatever it says.
        checks = {
            "workflow_completed": bool(output),
            "tool_was_called": bool(calls),
            "no_tool_call_executed_twice": all(r[1] == 1 for r in calls),
            "tool_activities_all_on_io": bool(tool_acts) and all(a["queue"] == TOOL_QUEUE for a in tool_acts),
            "model_activities_all_on_llm": bool(model_acts) and all(a["queue"] == MODEL_QUEUE for a in model_acts),
            "completed_model_requests_not_rerun": len([a for a in model_acts if a["done"]]) == len(model_acts),
        }
    else:
      checks = {
        "workflow_completed": output == "done",
        "each_call_no_has_one_row": [r[0] for r in calls] == [1, 2],
        "each_call_executed_exactly_once": all(r[1] == 1 for r in calls),
        "turn1_model_invoked_once_across_kill": dict(models).get(1) == 1,
        "tool_activities_all_on_io": bool(tool_acts) and all(a["queue"] == TOOL_QUEUE for a in tool_acts),
        "model_activities_all_on_llm": bool(model_acts) and all(a["queue"] == MODEL_QUEUE for a in model_acts),
        # turn 1 ran before the kill, turn 3 after the restart: different pids prove a real process boundary.
        "resumed_in_new_process": pids.get(1) is not None and pids.get(1) != pids.get(3),
      }
    summary = {
        "api": API,
        "run_id": run_id,
        "killed_after": killed_after,
        "spike_calls": calls,
        "model_invocations_per_turn": models,
        "model_pid_per_turn": pids,
        "activities": acts,
        "checks": checks,
        "reproduce": f"./run.sh uv run python g1_durable_agent.py --api {API}" + (f" --real {REAL}" if REAL else ""),
        "real_model": REAL,
    }
    os.makedirs("out", exist_ok=True)
    with open(f"out/g1-{API}{'-real' if REAL else ''}.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(json.dumps(summary, indent=2, default=str))
    ok = all(checks.values())
    print("G1 (" + API + "):", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- openrouter leg


async def openrouter_leg() -> dict:
    """Which OpenRouter models actually call a tool. Skipped without a key."""
    key = os.environ.get("OPENROUTER_API_KEY")
    models = [m for m in os.environ.get("G1_OPENROUTER_MODELS", "").split(",") if m]
    if not key or not models:
        return {"status": "BLOCKED", "reason": "OPENROUTER_API_KEY and/or G1_OPENROUTER_MODELS not set"}
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    results = {}
    for name in models:
        calls: list[int] = []
        a = Agent(OpenAIChatModel(name, provider=OpenAIProvider(base_url=base, api_key=key)))

        @a.tool_plain
        def lookup(n: int) -> str:
            """Return the square of n."""
            calls.append(n)
            return str(n * n)

        try:
            r = await a.run("Use the lookup tool to get the square of 12, then reply with the number only.")
            results[name] = {"called_tool": bool(calls), "output": str(r.output)[:60]}
        except Exception as e:  # noqa: BLE001 - a spike records failures, it does not hide them
            results[name] = {"called_tool": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"status": "RAN", "models": results}


if __name__ == "__main__":
    mode = next((a for a in sys.argv[1:] if a in ("worker", "openrouter")), "orchestrate")
    if mode == "worker":
        asyncio.run(worker_main())
    elif mode == "openrouter":
        print(json.dumps(asyncio.run(openrouter_leg()), indent=2))
    else:
        sys.exit(asyncio.run(orchestrate()))

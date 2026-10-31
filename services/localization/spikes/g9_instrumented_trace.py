"""G9 (a copy of G7 with tracing): what Langfuse shows when pydantic-ai instrumentation is on under Temporal.

Run: ./run.sh uv run python g9_instrumented_trace.py kill   (LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in the environment)
Then read the trace back from Langfuse: names, nesting, input and output of each span, duplicates after the replay.

Service mode: the service's own tracing (its interceptor, replay-safe provider, agent
instrumentation, cost on the request span, redacting exporter) around the real workflow
with the Dubber in `code` mode, on the host, against a local Temporal dev server and the
real Langfuse. The worker is SIGKILLed while the code's `synthesize` calls are in flight
and a new one finishes the run; the trace is then read back through the public API and
printed as a tree. The model is scripted (`CodeDubber`) and each request gets a priced
OpenRouter-shaped reply from a local stub through the production client.

  cd services/localization && uv run python spikes/g9_instrumented_trace.py service
  (reads LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY from the repo .env; Langfuse on 127.0.0.1:3100)

G7 text follows.

    G7: can the Dubber's tools be called from Pydantic AI CodeMode (model-written code run
in the Monty sandbox) and keep what Temporal gives the current design?

Questions answered, each with a measurement rather than an assumption:
  Q1  Is every nested tool call its own Temporal activity, on the tool's own task queue?
  Q2  Do calls gathered in the sandbox really overlap in time?
  Q3  After a worker SIGKILL, are nested calls that had completed run again?
  Q4  Does a guard that counts ToolCallParts in the history (today's RewriteLimit) still
      stop a fourth call when the calls are made from code? Does a guard inside the tool?

Run (inside the compose network):
  ./run.sh uv run python g7_code_mode.py kill
  ./run.sh uv run python g7_code_mode.py guard --guard history
  ./run.sh uv run python g7_code_mode.py guard --guard tool

The model is a scripted FunctionModel, so only the framework is measured, not a provider.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from typing import Any

import base64

import psycopg
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic_ai.models.instrumented import InstrumentationSettings
from temporalio.contrib.opentelemetry import TracingInterceptor, create_tracer_provider
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec.temporal import (
    AgentPlugin,
    PydanticAIPlugin,
    PydanticAIWorkflow,
    TemporalDurability,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai_harness import CodeMode
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig

SCENARIO = next((a for a in sys.argv[1:] if a in ("kill", "guard")), "kill")
GUARD = sys.argv[sys.argv.index("--guard") + 1] if "--guard" in sys.argv else "history"
ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
PGUSER = os.environ.get("POSTGRES_USER", "reup")
PGPASSWORD = os.environ.get("POSTGRES_PASSWORD", "reup")
DB = "spike_g9"
DB_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@postgres:5432/{DB}"
ADMIN_URL = f"postgresql://{PGUSER}:{PGPASSWORD}@postgres:5432/postgres"

MODEL_QUEUE, TOOL_QUEUE, WORKFLOW_QUEUE = "g9-llm", "g9-tts", "g9-workflows"
FAST_SECONDS = 1.5  # long enough that overlap cannot be an accident of scheduling
SLOW_SECONDS = 6.0  # the window in which the worker is killed
MAX_REWRITES = 3

# Monty code the scripted model "writes". Three calls gathered, then one slow call.
KILL_CODE = """\
import asyncio
first = await asyncio.gather(
    synthesize(id=0, text_vi="a"),
    synthesize(id=1, text_vi="b"),
    synthesize(id=2, text_vi="c"),
)
last = await synthesize(id=3, text_vi="d")
{"first": first, "last": last}
"""

# Five calls for one segment, each outcome recorded. Today's guard allows three.
GUARD_CODE = """\
results = []
for n in range(5):
    try:
        results.append(await synthesize(id=7, text_vi="line " + str(n)))
    except Exception as e:
        results.append("ERR " + str(e)[:80])
results
"""


def _run_id_from(messages: list[ModelMessage]) -> str:
    for m in messages:
        for p in getattr(m, "parts", []):
            if isinstance(p, UserPromptPart) and isinstance(p.content, str) and "run_id=" in p.content:
                return p.content.split("run_id=", 1)[1].split()[0]
    raise RuntimeError("run_id not found in prompt")


async def _scripted_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    turn = 1 + sum(isinstance(p, ToolReturnPart) for m in messages for p in getattr(m, "parts", []))
    if turn == 1:
        code = KILL_CODE if SCENARIO == "kill" else GUARD_CODE
        return ModelResponse(parts=[ToolCallPart("run_code", {"code": code})])
    returns = [
        p.content for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
    ]
    return ModelResponse(parts=[TextPart(json.dumps(returns, default=str))])


async def synthesize(ctx: RunContext[str], id: int, text_vi: str) -> str:
    """Speaks `text_vi` as the new line of segment `id`. Costs a TTS call."""
    run_id = ctx.deps
    async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as c:
        if GUARD == "tool" and SCENARIO == "guard":
            (used,) = await (
                await c.execute("SELECT count(*) FROM tool_calls WHERE run_id=%s AND seg=%s", (run_id, id))
            ).fetchone()
            if used >= MAX_REWRITES:
                raise ModelRetry(f"segment {id} has used its {MAX_REWRITES} rewrites")
        row = await (
            await c.execute(
                "INSERT INTO tool_calls(run_id, seg, pid, t_start) VALUES (%s,%s,%s,clock_timestamp()) RETURNING n",
                (run_id, id, os.getpid()),
            )
        ).fetchone()
        await asyncio.sleep(SLOW_SECONDS if id == 3 else FAST_SECONDS if SCENARIO == "kill" else 0.05)
        await c.execute("UPDATE tool_calls SET t_end=clock_timestamp() WHERE n=%s", (row[0],))
    return f"segment {id} spoken"


class HistoryGuard(AbstractCapability[str]):
    """Today's RewriteLimit: the count is read from ToolCallParts in the conversation."""

    async def before_tool_execute(
        self, ctx: RunContext[str], *, call: ToolCallPart, tool_def: Any, args: Any
    ) -> Any:
        if call.tool_name != "synthesize":
            return args
        wanted = args.get("id") if isinstance(args, dict) else None
        spoken = sum(
            1
            for m in ctx.messages
            if isinstance(m, ModelResponse)
            for p in m.parts
            if isinstance(p, ToolCallPart)
            and p.tool_name == "synthesize"
            and p.args_as_dict().get("id") == wanted
        )
        if spoken > MAX_REWRITES:
            raise ModelRetry(f"segment {wanted} has used its {MAX_REWRITES} rewrites")
        return args


_tool_cfg = ActivityConfig(task_queue=TOOL_QUEUE, start_to_close_timeout=timedelta(seconds=60))
_model_cfg = ActivityConfig(task_queue=MODEL_QUEUE, start_to_close_timeout=timedelta(seconds=60))
_base_cfg = ActivityConfig(start_to_close_timeout=timedelta(seconds=60))

agent: Agent[str, str] = Agent(
    FunctionModel(_scripted_model),
    name="g9",
    deps_type=str,
    capabilities=[
        HistoryGuard(),
        CodeMode(),
        TemporalDurability(activity_config=_base_cfg, model_activity_config=_model_cfg),
    ],
)
agent.tool(synthesize, name="synthesize", metadata={"temporal": _tool_cfg})


@workflow.defn(name="G9Workflow")
class G9Workflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [agent]

    @workflow.run
    async def run(self, run_id: str) -> str:
        result = await agent.run(f"run_id={run_id}", deps=run_id)
        return result.output



def setup_tracing(service: str):
    """Replay-safe provider exporting to Langfuse; pydantic-ai instrumentation on for every agent."""
    token = base64.b64encode(f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}".encode()).decode()
    host = os.environ.get("LANGFUSE_INTERNAL_HOST", "http://langfuse-web:3000").rstrip("/")
    provider = create_tracer_provider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{host}/api/public/otel/v1/traces",
                headers={"Authorization": f"Basic {token}", "x-langfuse-ingestion-version": "4"},
            ),
            schedule_delay_millis=500,
        )
    )
    trace.set_tracer_provider(provider)
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=True))
    return provider


# --------------------------------------------------------------------------- worker


async def worker_main() -> None:
    import signal

    provider = setup_tracing("g9-worker")
    signal.signal(signal.SIGTERM, lambda *_: (provider.force_flush(), os._exit(0)))
    mod = __import__("g9_instrumented_trace")
    client = await Client.connect(ADDRESS, namespace=NAMESPACE, plugins=[PydanticAIPlugin()], interceptors=[TracingInterceptor()])
    workers = [Worker(client, task_queue=WORKFLOW_QUEUE, workflows=[mod.G9Workflow])]
    workers += [
        Worker(client, task_queue=q, plugins=[AgentPlugin(mod.agent)]) for q in (MODEL_QUEUE, TOOL_QUEUE)
    ]
    await asyncio.gather(*(w.run() for w in workers))


# --------------------------------------------------------------------------- orchestrator


def _setup_db() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {DB}")
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute(
            "CREATE TABLE tool_calls(n serial PRIMARY KEY, run_id text, seg int, pid int,"
            " t_start timestamptz, t_end timestamptz)"
        )


def _spawn_worker() -> subprocess.Popen:
    extra = ["--guard", GUARD] if SCENARIO == "guard" else []
    return subprocess.Popen([sys.executable, __file__, "worker", SCENARIO, *extra])


async def _activities(handle) -> list[dict]:
    from temporalio.api.enums.v1 import EventType

    events = [e async for e in handle.fetch_history_events()]
    scheduled: dict[int, dict] = {}
    for e in events:
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            a = e.activity_task_scheduled_event_attributes
            scheduled[e.event_id] = {"type": a.activity_type.name, "queue": a.task_queue.name, "done": False}
        elif e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            sid = e.activity_task_completed_event_attributes.scheduled_event_id
            if sid in scheduled:
                scheduled[sid]["done"] = True
    return list(scheduled.values())


def _max_overlap(rows: list[tuple]) -> int:
    # rows are (seg, pid, t_start, t_end)
    events = sorted([(r[2], 1) for r in rows if r[2]] + [(r[3], -1) for r in rows if r[3]])
    cur = best = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


async def orchestrate() -> int:
    _setup_db()
    provider = setup_tracing("g9-orchestrator")
    client = await Client.connect(ADDRESS, namespace=NAMESPACE, plugins=[PydanticAIPlugin()], interceptors=[TracingInterceptor()])
    run_id = f"g9-{SCENARIO}-{int(time.time())}"
    worker = _spawn_worker()
    await asyncio.sleep(4)
    handle = await client.start_workflow("G9Workflow", run_id, id=run_id, task_queue=WORKFLOW_QUEUE)

    killed_after = None
    if SCENARIO == "kill":
        deadline = time.time() + 90
        while time.time() < deadline and killed_after is None:
            # Kill while the slow fourth call is running: it has started and not ended.
            with psycopg.connect(DB_URL, autocommit=True) as c:
                (in_flight,) = c.execute(
                    "SELECT count(*) FROM tool_calls WHERE seg=3 AND t_start IS NOT NULL AND t_end IS NULL"
                ).fetchone()
                (finished,) = c.execute("SELECT count(*) FROM tool_calls WHERE t_end IS NOT NULL").fetchone()
            if in_flight and finished >= 3:
                worker.send_signal(signal.SIGKILL)
                killed_after = finished
            await asyncio.sleep(0.05)
        if killed_after is None:
            print("FAIL: never saw the slow call in flight after three completed ones")
            worker.kill()
            return 1
        worker.wait()
        time.sleep(1)
        worker = _spawn_worker()
    try:
        output = await asyncio.wait_for(handle.result(), timeout=180)
    except Exception as e:  # noqa: BLE001 - a spike records the failure instead of hiding it
        output = f"WORKFLOW FAILED: {type(e).__name__}: {str(e)[:300]}"
    worker.send_signal(signal.SIGTERM)
    worker.wait()

    acts = await _activities(handle)
    with psycopg.connect(DB_URL) as c:
        rows = c.execute("SELECT seg, pid, t_start, t_end FROM tool_calls ORDER BY n").fetchall()
    per_seg: dict[int, int] = {}
    for r in rows:
        per_seg[r[0]] = per_seg.get(r[0], 0) + 1
    tool_acts = [a for a in acts if a["queue"] == TOOL_QUEUE]

    if SCENARIO == "kill":
        first_three = [r for r in rows if r[0] in (0, 1, 2)]
        checks = {
            "workflow_completed": isinstance(output, str) and not output.startswith("WORKFLOW FAILED"),
            "q1_each_nested_call_is_an_activity_on_tool_queue": len(tool_acts) >= 4,
            "q2_gathered_calls_overlapped": _max_overlap(first_three) == 3,
            "q3_completed_calls_not_rerun": all(per_seg.get(s) == 1 for s in (0, 1, 2)),
            # killed mid-flight: Temporal is at-least-once, so the call ran again exactly once more
            "q3_in_flight_call_retried_after_kill": per_seg.get(3) == 2,
            "resumed_in_new_process": len({r[1] for r in rows}) >= 2,
        }
    else:
        executed = per_seg.get(7, 0)
        checks = {
            "workflow_completed": isinstance(output, str) and not output.startswith("WORKFLOW FAILED"),
            "q4_fourth_and_fifth_call_were_refused": executed == MAX_REWRITES,
        }
    summary = {
        "scenario": SCENARIO,
        "guard": GUARD if SCENARIO == "guard" else None,
        "killed_after_completed_tool_activities": killed_after,
        "tool_executions_per_segment": per_seg,
        "max_overlap_of_first_three": _max_overlap([r for r in rows if r[0] in (0, 1, 2)]),
        "activities": acts,
        "workflow_output": output,
        "checks": checks,
        "reproduce": f"./run.sh uv run python g7_code_mode.py {SCENARIO}" + (f" --guard {GUARD}" if SCENARIO == "guard" else ""),
    }
    provider.force_flush()
    print("RUN_ID", run_id)
    os.makedirs("out", exist_ok=True)
    name = f"g9-{SCENARIO}" + (f"-{GUARD}" if SCENARIO == "guard" else "")
    with open(f"out/{name}.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(json.dumps(summary, indent=2, default=str))
    ok = all(checks.values())
    print(f"G7 ({name}):", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# --------------------------------------------------------------------------- service mode

SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGFUSE_HOST = os.environ.get("G9_LANGFUSE_HOST", "http://127.0.0.1:3100")
SPEAK_SECONDS = 6.0


def _service_settings():  # noqa: ANN202
    """Settings with only Langfuse's keys, read from the repo .env (never printed)."""
    sys.path.insert(0, SERVICE_ROOT)
    from dotenv import dotenv_values

    from localization.config import Settings

    env = dotenv_values(os.path.join(SERVICE_ROOT, "..", "..", ".env"))
    return Settings(
        langfuse_host=LANGFUSE_HOST,
        langfuse_public_key=env["LANGFUSE_PUBLIC_KEY"],
        langfuse_secret_key=env["LANGFUSE_SECRET_KEY"],
        trace_agent_content=os.environ.get("TRACE_AGENT_CONTENT", "true") != "false",
    )


async def service_worker(address: str, state: str) -> None:
    settings = _service_settings()
    from localization.observability.agent_tracing import instrument_agents
    from localization.observability.tracing import init_tracing
    from localization.providers.llm import openrouter_client
    from localization.temporal.client import connect
    from tests.observability.test_agent_traces import PricedDubber
    from tests.support.dub_fakes import DubFakes, FakeDubberPorts
    from tests.support.localizer_fakes import LocalizerFakes
    from tests.support.prep_fakes import PrepFakes
    from tests.support.prep_worker import prep_workers
    from tests.support.stub_server import StubServer

    provider = init_tracing(settings, f"g9-service-worker-{os.getpid()}")
    assert provider is not None
    instrument_agents(provider, include_content=settings.trace_agent_content)
    signal.signal(signal.SIGTERM, lambda *_: (provider.force_flush(), os._exit(0)))

    class MarkedPorts(FakeDubberPorts):
        async def speak(self, deps, text_vi):  # noqa: ANN001, ANN201
            open(os.path.join(state, "speaking"), "a").write(f"{os.getpid()} {text_vi}\n")
            return await super().speak(deps, text_vi)

    stub = StubServer().start()
    client = await connect(address)
    async with openrouter_client() as http:
        priced = PricedDubber(stub, http)
        ports = MarkedPorts(speak_seconds=SPEAK_SECONDS)
        dubbing = DubFakes(unfit_ids=(0, 1), mode="code", ports=ports, dubber=priced)
        async with prep_workers(client, PrepFakes(), LocalizerFakes(), dubbing=dubbing):
            open(os.path.join(state, f"ready-{os.getpid()}"), "w").close()
            await asyncio.Event().wait()


def _spawn_service_worker(address: str, state: str) -> subprocess.Popen:
    worker = subprocess.Popen(
        [sys.executable, __file__, "service-worker", address, state], cwd=SERVICE_ROOT
    )
    deadline = time.time() + 60
    while not os.path.exists(os.path.join(state, f"ready-{worker.pid}")):
        if time.time() > deadline or worker.poll() is not None:
            raise RuntimeError("the service worker did not start")
        time.sleep(0.1)
    return worker


def _langfuse_get(path: str, **params: object) -> dict:
    import httpx

    settings = _service_settings()
    auth = (
        settings.langfuse_public_key.get_secret_value(),
        settings.langfuse_secret_key.get_secret_value(),
    )
    response = httpx.get(f"{LANGFUSE_HOST}{path}", params=params, auth=auth, timeout=30)
    response.raise_for_status()
    return response.json()


def _observations(trace_id: str) -> list[dict]:
    found, page = [], 1
    while True:
        body = _langfuse_get("/api/public/observations", traceId=trace_id, page=page, limit=100)
        found += body["data"]
        if page >= body["meta"]["totalPages"]:
            return found
        page += 1


def _tree(observations: list[dict]) -> list[str]:
    ids = {o["id"] for o in observations}
    children: dict[str | None, list[dict]] = {}
    for o in observations:
        parent = o.get("parentObservationId")
        children.setdefault(parent if parent in ids else None, []).append(o)
    lines: list[str] = []

    def walk(parent: str | None, depth: int) -> None:
        for o in sorted(children.get(parent, []), key=lambda o: o["startTime"]):
            marks = ("in" if o.get("input") not in (None, "", {}) else "--") + "/" + (
                "out" if o.get("output") not in (None, "", {}) else "---"
            )
            extra = ""
            usage, cost = o.get("usageDetails") or {}, o.get("costDetails") or {}
            if usage or cost:
                extra = f"  tokens={usage.get('total')} cost={cost.get('total')}"
            lines.append(f"{'  ' * depth}{o['name']}  [{o['type']}] {marks}{extra}")
            walk(o["id"], depth + 1)

    walk(None, 0)
    return lines


async def service_orchestrate() -> int:
    sys.path.insert(0, SERVICE_ROOT)
    import tempfile

    from temporalio.testing import WorkflowEnvironment

    from localization.contracts import LocalizationResult
    from localization.temporal.client import connect
    from tests.support.inputs import make_input

    aweme = str(int(time.time() * 1000))  # the run's own Langfuse session
    state = tempfile.mkdtemp(prefix="g9-service-")
    env = await WorkflowEnvironment.start_local()
    try:
        address = env.client.service_client.config.target_host
        client = await connect(address)
        worker = _spawn_service_worker(address, state)
        handle = await client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(aweme_id=aweme),
            id=f"video-{aweme}-g1",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        speaking = os.path.join(state, "speaking")
        deadline = time.time() + 90
        while not (os.path.exists(speaking) and len(open(speaking).read().splitlines()) >= 2):
            if time.time() > deadline:
                raise RuntimeError("the code never started speaking")
            await asyncio.sleep(0.05)
        await asyncio.sleep(1.0)
        worker.send_signal(signal.SIGKILL)
        worker.wait()
        killed = worker.returncode
        worker = _spawn_service_worker(address, state)
        result = await asyncio.wait_for(handle.result(), timeout=180)
        worker.send_signal(signal.SIGTERM)
        worker.wait()
    finally:
        await env.shutdown()
    speaks = open(os.path.join(state, "speaking")).read().splitlines()
    print("workflow", type(result).__name__, "worker 1 exit code", killed)
    print("speak calls (pid text):", speaks)

    traces: list[dict] = []
    for _ in range(60):  # Langfuse ingests asynchronously
        traces = _langfuse_get("/api/public/traces", sessionId=aweme)["data"]
        observations = _observations(traces[0]["id"]) if traces else []
        if any(o["name"] == "RunWorkflow:VideoLocalizationWorkflow" for o in observations):
            break
        await asyncio.sleep(2)
    print("session", aweme, "traces", [t["id"] for t in traces])
    observations = _observations(traces[0]["id"])
    lines = _tree(observations)
    print("\n".join(lines))
    names = collections.Counter(o["name"] for o in observations)
    priced = [o for o in observations if (o.get("costDetails") or {}).get("total")]
    summary = {
        "content_capture": _service_settings().trace_agent_content,
        "workflow_result": type(result).__name__,
        "first_worker_exit_code": killed,
        "speak_calls_pid_text": speaks,
        "trace_id": traces[0]["id"],
        "nodes": len(observations),
        "by_name": dict(names),
        "observations_with_cost": [(o["name"], o["costDetails"]) for o in priced],
        "temporal_wrappers": [n for n in names if n.startswith(("Start", "Complete"))]
        + [n for n in names if n.startswith("RunActivity:agent__")],
    }
    out = os.path.join(SERVICE_ROOT, "spikes", "out")
    os.makedirs(out, exist_ok=True)
    name = "g9-service.json" if summary["content_capture"] else "g9-service-no-content.json"
    with open(os.path.join(out, name), "w") as f:
        json.dump({**summary, "tree": lines}, f, indent=2, ensure_ascii=False)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if "service-worker" in sys.argv[1:]:
        asyncio.run(service_worker(sys.argv[2], sys.argv[3]))
    elif "service" in sys.argv[1:]:
        sys.exit(asyncio.run(service_orchestrate()))
    elif "worker" in sys.argv[1:]:
        asyncio.run(worker_main())
    else:
        sys.exit(asyncio.run(orchestrate()))

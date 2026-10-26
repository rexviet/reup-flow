"""G3: does one workflow run produce exactly one Langfuse trace, containing the
Node worker's span, and does a worker kill + resume leave the span count
unchanged (no spans born from workflow code during replay)?

Run:  ./run.sh uv run python g3_trace.py

Two runs of the same workflow: A uninterrupted, B with the Python worker
SIGKILLed after the first activity. Both are compared by span-name multiset.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.contrib.opentelemetry import TracingInterceptor
from temporalio.worker import Worker

# The sandbox re-imports this module to validate the workflow, and rejects
# urllib.request (pulled in by httpx and the OTLP exporter). None of these are
# used by workflow code, so they pass through.
with workflow.unsafe.imports_passed_through():
    import httpx
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

KILL_RUNS = 3
ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
LANGFUSE = os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000")
PK, SK = os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
AUTH = base64.b64encode(f"{PK}:{SK}".encode()).decode()


class _JsonlExporter(SpanExporter):
    """Local ground truth: every span this process ended, regardless of whether
    the OTLP export or Langfuse ingestion later drops it."""

    def export(self, spans):
        os.makedirs("out", exist_ok=True)
        with open("out/g3-spans.jsonl", "a") as f:
            for sp in spans:
                f.write(json.dumps({"pid": os.getpid(), "name": sp.name, "trace_id": format(sp.context.trace_id, "032x")}) + "\n")
        return SpanExportResult.SUCCESS


def _init_tracing() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "localization-worker"}))
    # BatchSpanProcessor, never SimpleSpanProcessor: spans for workflow code end
    # inside the Temporal sandbox thread, and Simple exports synchronously right
    # there, where the sandbox forbids the I/O ("Cannot access __builtins__.open
    # from inside a workflow") and the span is silently dropped. Batch exports
    # from its own thread. A short delay keeps the SIGKILL loss window small.
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{LANGFUSE}/api/public/otel/v1/traces",
                headers={"Authorization": f"Basic {AUTH}", "x-langfuse-ingestion-version": "4"},
            ),
            schedule_delay_millis=100,
        )
    )
    provider.add_span_processor(BatchSpanProcessor(_JsonlExporter(), schedule_delay_millis=100))
    trace.set_tracer_provider(provider)


@activity.defn(name="py_step")
async def py_step(run_id: str) -> str:
    return f"step-{run_id}"


@activity.defn(name="py_slow")
async def py_slow(run_id: str) -> str:
    await asyncio.sleep(4)  # the window in which run B's worker is killed
    return "slow"


@workflow.defn(name="G3Workflow")
class G3Workflow:
    @workflow.run
    async def run(self, run_id: str) -> str:
        t = timedelta(seconds=30)
        await workflow.execute_activity("py_step", run_id, task_queue="media", start_to_close_timeout=t)
        await workflow.execute_activity("py_slow", run_id, task_queue="media", start_to_close_timeout=t)
        return await workflow.execute_activity("render", run_id, task_queue="render", start_to_close_timeout=t)


async def _client() -> Client:
    return await Client.connect(ADDRESS, namespace=NAMESPACE, interceptors=[TracingInterceptor()])


async def worker_main() -> None:
    _init_tracing()
    mod = __import__("g3_trace")
    client = await _client()
    await asyncio.gather(
        Worker(client, task_queue="workflows", workflows=[mod.G3Workflow]).run(),
        Worker(client, task_queue="media", activities=[mod.py_step, mod.py_slow]).run(),
    )


# --------------------------------------------------------------------------- orchestrator


def _spawn(*cmd: str) -> subprocess.Popen:
    return subprocess.Popen(list(cmd))


async def _scheduled_types(handle) -> list[str]:
    from temporalio.api.enums.v1 import EventType

    events = [e async for e in handle.fetch_history_events()]
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


async def _py_step_done(handle) -> bool:
    from temporalio.api.enums.v1 import EventType

    events = [e async for e in handle.fetch_history_events()]
    return any(e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED for e in events)


async def _one_run(client: Client, tag: str, kill: bool) -> dict:
    tracer = trace.get_tracer("g3-orchestrator")
    run_id = f"g3-{tag}-{int(time.time())}"
    py = _spawn(sys.executable, __file__, "worker")
    await asyncio.sleep(5)
    killed = False
    scheduled_at_kill: list[str] = []
    with tracer.start_as_current_span(f"g3-run-{tag}") as root:
        trace_id = format(root.get_span_context().trace_id, "032x")
        handle = await client.start_workflow("G3Workflow", run_id, id=run_id, task_queue="workflows")
        if kill:
            deadline = time.time() + 60
            while time.time() < deadline:
                if await _py_step_done(handle):
                    py.send_signal(signal.SIGKILL)
                    killed = True
                    scheduled_at_kill = await _scheduled_types(handle)
                    break
                await asyncio.sleep(0.05)
            py.wait()
            time.sleep(1)
            py = _spawn(sys.executable, __file__, "worker")
        await asyncio.wait_for(handle.result(), timeout=120)
    py.send_signal(signal.SIGTERM)
    py.wait()
    return {"tag": tag, "run_id": run_id, "trace_id": trace_id, "killed": killed, "scheduled_at_kill": scheduled_at_kill}


def _fetch_trace(trace_id: str) -> dict | None:
    r = httpx.get(f"{LANGFUSE}/api/public/traces/{trace_id}", headers={"Authorization": f"Basic {AUTH}"}, timeout=30)
    return r.json() if r.status_code == 200 else None


def _list_traces(since: datetime) -> list[dict]:
    r = httpx.get(
        f"{LANGFUSE}/api/public/traces",
        params={"fromTimestamp": since.isoformat(), "limit": 100},
        headers={"Authorization": f"Basic {AUTH}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]


async def orchestrate() -> int:
    if os.path.exists("out/g3-spans.jsonl"):
        os.remove("out/g3-spans.jsonl")
    _init_tracing()
    subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--silent"], check=True)
    node = subprocess.Popen(["npx", "tsx", "g3_render_span.ts"])
    await asyncio.sleep(8)
    client = await _client()
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    try:
        a = await _one_run(client, "a", kill=False)
        bs = [await _one_run(client, f"b{i}", kill=True) for i in range(1, KILL_RUNS + 1)]
    finally:
        node.send_signal(signal.SIGTERM)
        node.wait()

    trace.get_tracer_provider().force_flush()
    # Langfuse ingests asynchronously (S3 event, queue delay, ClickHouse), and
    # lands spans in delayed batches. Counting early reads a half-ingested trace
    # as "missing spans", so poll until every run's span count has been stable
    # for several consecutive polls.
    runs = [a, *bs]
    fetched: dict[str, dict | None] = {}
    last: dict[str, int] = {}
    stable = 0
    deadline = time.time() + 300
    while time.time() < deadline:
        fetched = {r["tag"]: _fetch_trace(r["trace_id"]) for r in runs}
        counts = {k: len(t["observations"]) if t else 0 for k, t in fetched.items()}
        stable = stable + 1 if counts == last and all(counts.values()) else 0
        last = counts
        if stable >= 4:
            break
        time.sleep(10)

    def _norm(name: str) -> str:
        return "g3-run" if name.startswith("g3-run-") else name  # the root span carries the run tag

    names = {
        tag: Counter(_norm(o["name"]) for o in (t or {"observations": []})["observations"]) for tag, t in fetched.items()
    }
    resources = {
        tag: sorted({(o.get("metadata") or {}).get("resourceAttributes", {}).get("service.name", "?") for o in t["observations"]} if t else [])
        for tag, t in fetched.items()
    }
    traces_in_window = _list_traces(since)
    ids = {t["id"] for t in traces_in_window}
    # A replay that re-emitted workflow spans would ADD spans. A span that was
    # created but not yet exported when SIGKILL landed is lost for good (replay
    # suppresses it), so the B runs may legitimately have FEWER. Gate on extras;
    # report the missing ones as a known loss.
    extra = {r["tag"]: dict(names[r["tag"]] - names["a"]) for r in bs}
    missing = {r["tag"]: dict(names["a"] - names[r["tag"]]) for r in bs}
    local: dict[str, Counter] = {r["tag"]: Counter() for r in runs}
    by_trace = {r["trace_id"]: r["tag"] for r in runs}
    with open("out/g3-spans.jsonl") as f:
        for line in f:
            row = json.loads(line)
            if row["trace_id"] in by_trace:
                local[by_trace[row["trace_id"]]][_norm(row["name"])] += 1
    local_extra = {r["tag"]: dict(local[r["tag"]] - local["a"]) for r in bs}
    local_missing = {r["tag"]: dict(local["a"] - local[r["tag"]]) for r in bs}
    checks = {
        "python_side_emits_no_extra_spans_after_kill": all(not e for e in local_extra.values()),
        "every_run_has_a_trace": all(fetched.get(r["tag"]) is not None for r in runs),
        "exactly_the_expected_traces_exist": ids == {r["trace_id"] for r in runs},
        "node_span_present_in_every_run": all(any(n.startswith("RunActivity:render") for n in names[r["tag"]]) for r in runs),
        "kill_and_resume_adds_no_spans": all(not e for e in extra.values()),
    }
    b = bs  # noqa: F841 - kept for the summary block below
    summary = {
        "runs": runs,
        "span_names": {k: dict(v) for k, v in names.items()},
        "span_total": {k: sum(v.values()) for k, v in names.items()},
        "python_side_spans_emitted_locally": {k: dict(v) for k, v in local.items()},
        "python_side_extra_vs_uninterrupted": local_extra,
        "python_side_missing_vs_uninterrupted": local_missing,
        "extra_vs_uninterrupted": extra,
        "missing_vs_uninterrupted": missing,
        "service_names_seen": resources,
        "traces_in_window": len(ids),
        "checks": checks,
        "reproduce": "./run.sh uv run python g3_trace.py",
    }
    os.makedirs("out", exist_ok=True)
    with open("out/g3.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    ok = all(checks.values())
    print("G3:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "worker" in sys.argv[1:]:
        asyncio.run(worker_main())
    else:
        sys.exit(asyncio.run(orchestrate()))

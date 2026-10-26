"""G6: how large does the biggest model-request activity input get for a
Localizer working a 3-minute video through TemporalDurability?

Temporal rejects a payload over 2MB and warns at 256KB; the plan's own guard
fails at 256KB, and the gate threshold is 200KB. Every model request re-sends
the whole message history, so the tool results (the transcript) are what grow it.

Run:  ./run.sh uv run python g6_history_size.py

The transcript is SYNTHETIC (no STT is wired up in phase 00): ~3 minutes of
speech with word-level timestamps, sized from real Mandarin short-video pace.
The measurement of the activity inputs is real, taken from Temporal history.
Two densities are measured; the gate reads the realistic one and the stress
one is reported so a denser video is not a surprise.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
from datetime import timedelta

from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import (
    AgentPlugin,
    PydanticAIPlugin,
    PydanticAIWorkflow,
    TemporalDurability,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.workflow import ActivityConfig

ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "temporal:7233")
NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
THRESHOLD_BYTES = 200 * 1024
BATCH = 10  # segments per estimate_duration call: how a Localizer is expected to batch

# Pace of short-form Mandarin speech and of its Vietnamese rendering.
DENSITY = {"realistic": 4.0, "stress": 8.0}  # Chinese characters per second

VI_SYLLABLES = "một hai ba bốn năm sáu bảy tám chín mười người làm việc nhà trong ngày hôm nay chúng ta cùng xem thử cách nấu món ăn ngon".split()


def make_transcript(seconds: int, chars_per_second: float) -> list[dict]:
    """Segments of ~5 s, each carrying per-character word timestamps."""
    rng = random.Random(7)
    segments, t, i = [], 0.0, 0
    while t < seconds:
        dur = rng.uniform(3.5, 6.5)
        n = max(4, int(dur * chars_per_second))
        step = dur / n
        segments.append(
            {
                "id": i,
                "start": round(t, 3),
                "end": round(t + dur, 3),
                "text": "".join(chr(0x4E00 + rng.randrange(0, 2000)) for _ in range(n)),
                "words": [
                    {"word": chr(0x4E00 + rng.randrange(0, 2000)), "start": round(t + k * step, 3), "end": round(t + (k + 1) * step, 3)}
                    for k in range(n)
                ],
            }
        )
        t += dur
        i += 1
    return segments


TRANSCRIPTS: dict[str, list[dict]] = {}


async def read_transcript(video: str) -> str:
    """The Localizer's real tool: read the transcript artifact and return it as JSON text."""
    return json.dumps(TRANSCRIPTS[video], ensure_ascii=False)


async def estimate_duration(texts: list[str]) -> list[float]:
    """Stand-in for the D6 syllable counter: seconds each Vietnamese line would take."""
    return [round(len(t.split()) / 4.5, 2) for t in texts]


def _vi_line(rng: random.Random, n_cn: int) -> str:
    return " ".join(rng.choice(VI_SYLLABLES) for _ in range(max(4, int(n_cn * 1.3))))


async def _localizer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
    video = next(
        p.content.split("video=", 1)[1].split()[0]
        for m in messages
        for p in getattr(m, "parts", [])
        if isinstance(p, UserPromptPart) and isinstance(p.content, str) and "video=" in p.content
    )
    if not returns:
        return ModelResponse(parts=[ToolCallPart("read_transcript", {"video": video})])
    segments = json.loads(str(returns[0].content))
    rng = random.Random(11)
    drafted = [_vi_line(rng, len(s["text"])) for s in segments]
    done_batches = len(returns) - 1
    if done_batches * BATCH < len(segments):
        batch = drafted[done_batches * BATCH : (done_batches + 1) * BATCH]
        return ModelResponse(parts=[ToolCallPart("estimate_duration", {"texts": batch})])
    return ModelResponse(parts=[TextPart(json.dumps(drafted, ensure_ascii=False))])


_cfg = ActivityConfig(start_to_close_timeout=timedelta(seconds=60))
agent = Agent(
    FunctionModel(_localizer),
    name="g6",
    capabilities=[
        TemporalDurability(
            activity_config=_cfg,
            model_activity_config=ActivityConfig(task_queue="llm", start_to_close_timeout=timedelta(seconds=60)),
        )
    ],
)
agent.tool_plain(read_transcript, metadata={"temporal": ActivityConfig(task_queue="io", start_to_close_timeout=timedelta(seconds=30))})
agent.tool_plain(estimate_duration, metadata={"temporal": ActivityConfig(task_queue="io", start_to_close_timeout=timedelta(seconds=30))})


@workflow.defn(name="G6Workflow")
class G6Workflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [agent]

    @workflow.run
    async def run(self, video: str) -> int:
        result = await agent.run(f"Localize this video. video={video}")
        return len(json.loads(result.output))


async def main() -> int:
    mod = __import__("g6_history_size")
    client = await Client.connect(ADDRESS, namespace=NAMESPACE, plugins=[PydanticAIPlugin()])
    workers = [Worker(client, task_queue="workflows", workflows=[mod.G6Workflow])] + [
        Worker(client, task_queue=q, plugins=[AgentPlugin(mod.agent)]) for q in ("llm", "io")
    ]
    runs = {}

    async def drive() -> None:
        for label, cps in DENSITY.items():
            video = f"v-{label}"
            mod.TRANSCRIPTS[video] = make_transcript(180, cps)
            wid = f"g6-{label}-{int(time.time())}"
            handle = await client.start_workflow("G6Workflow", video, id=wid, task_queue="workflows")
            n_segments = await asyncio.wait_for(handle.result(), timeout=180)
            from temporalio.api.enums.v1 import EventType

            sizes = []
            async for e in handle.fetch_history_events():
                if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                    a = e.activity_task_scheduled_event_attributes
                    total = sum(len(p.data) for p in a.input.payloads)
                    sizes.append({"type": a.activity_type.name, "bytes": total})
            model_inputs = [s["bytes"] for s in sizes if "model_request" in s["type"]]
            runs[label] = {
                "segments": n_segments,
                "transcript_json_bytes": len(json.dumps(mod.TRANSCRIPTS[video], ensure_ascii=False).encode()),
                "model_requests": len(model_inputs),
                "model_request_input_bytes": model_inputs,
                "max_model_request_input_bytes": max(model_inputs),
                "max_any_activity_input_bytes": max(s["bytes"] for s in sizes),
            }

    async def run_workers() -> None:
        await asyncio.gather(*(w.run() for w in workers))

    task = asyncio.create_task(run_workers())
    await asyncio.sleep(3)
    await drive()
    task.cancel()

    realistic = runs["realistic"]["max_model_request_input_bytes"]
    verdict = "PASS" if realistic < THRESHOLD_BYTES else "FAIL"
    summary = {
        "threshold_bytes": THRESHOLD_BYTES,
        "gate_reads": "realistic",
        "runs": runs,
        "stress_under_threshold": runs["stress"]["max_model_request_input_bytes"] < THRESHOLD_BYTES,
        "transcript_is_synthetic": True,
        "if_fail": "phase 05 must split the Localizer into 60-second windows",
        "verdict": verdict,
        "reproduce": "./run.sh uv run python g6_history_size.py",
    }
    os.makedirs("out", exist_ok=True)
    with open("out/g6.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"} | {"runs": {k: {kk: vv for kk, vv in v.items() if kk != "model_request_input_bytes"} for k, v in runs.items()}}, indent=2))
    print("G6:", verdict)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

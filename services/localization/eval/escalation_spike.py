"""Does handing a failed draft to a CodeMode agent beat the single repair request?

    uv run python eval/escalation_spike.py [--repeat 2] [--tight 1.0] [--model ID]

Each item is translated once with the production one-request answer. When `assess` finds
problems, the same draft goes down two arms, so they are compared on the same input:

  repair    the production arm: one repair request for the broken lines.
  repair2   a control: the repair request run a second time on what is still broken.
  escalate  a CodeMode agent: it writes one snippet that calls `check` with a few
            candidate rewrites per broken line (the production `assess` decides which pass),
            picks the first that passes, and answers.

A draft with no problems costs the same in both, so the policy "escalate only when
`assess` fails" adds latency only to the drafts that fail. The script reports how often
that is, how many problems each arm leaves, and what each arm costs in requests and time.
Wording quality is not judged here: both arms are scored by the same code checks only.

Data: the 41 synthetic cases and the Localizer contexts of two real runs (read from
their Temporal histories). `--tight` scales every slot, to stress the fit.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import TEMPORAL_UI, History, load_env
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import CodeMode
from run_experiment import _agents, _deps

from localization.agents import localizer_single
from localization.agents.answer_only import AnswerOnlyModel, answer_only_settings
from localization.agents.localizer import LocalizerDeps
from localization.agents.localizer_single import LocalizerContext
from localization.config import Settings
from localization.contracts.models import LocalizedLine, LocalizedLines
from localization.providers.llm import model_for, openrouter_client

REAL_RUNS = ("video-7616569292510948709-g9", "video-7618513827751629177-g9")
SYNTH = Path(__file__).parent / "scenarios" / "localizer-synth-v1.jsonl"

ESCALATE_INSTRUCTIONS = (
    localizer_single.REPAIR_INSTRUCTIONS
    + """
You also have a `check` function in the code sandbox. Write ONE Python snippet for run_code
that calls `await check({id: [candidate, candidate, candidate], ...})` ONCE, with up to
three candidate rewrites for every id in the request, from the least to the most shortened.
`check` says for each candidate either "ok" or what is wrong with it. For each id choose
the first candidate that is "ok"; if none is, the shortest one. Do not call `check` once per
candidate. End the snippet with the dict {id: chosen text}. If some ids still have no "ok"
candidate you may call `check` once more for those, then answer with one line per id.
"""
)


@dataclass
class Item:
    name: str
    deps: LocalizerDeps
    context: LocalizerContext


@dataclass
class Arm:
    seconds: float = 0.0
    requests: int = 0
    problems_after: int = 0
    unusable_after: int = 0
    error: str | None = None
    check_calls: int = 0
    candidates_checked: int = 0


@dataclass
class Result:
    item: str
    draft_seconds: float = 0.0
    problems_before: int = 0
    repair: Arm | None = None
    repair2: Arm | None = None
    escalate: Arm | None = None
    notes: list[str] = field(default_factory=list)


def _tighten(context: dict, factor: float) -> dict:
    if factor == 1.0:
        return context
    segments = [{**s, "slot_ms": max(200, int(s["slot_ms"] * factor))} for s in context["segments"]]
    return {**context, "segments": segments}


def load_items(tight: float) -> list[Item]:
    items: list[Item] = []
    for wid in REAL_RUNS:
        history = History.fetch(TEMPORAL_UI, wid)
        args, result = history.activities("localizer_context")[0]
        items.append(
            Item(
                wid.split("-")[1][-6:],
                LocalizerDeps.model_validate(args[0]),
                LocalizerContext.model_validate(_tighten(result, tight)),
            )
        )
    for line in SYNTH.read_text().splitlines():
        row = json.loads(line)
        inp = row["input"]
        items.append(
            Item(
                row["id"],
                _deps(inp, "escalation"),
                LocalizerContext.model_validate(_tighten(inp["context"], tight)),
            )
        )
    return items


def _replace(lines: LocalizedLines, line_id: int, text: str) -> LocalizedLines:
    return localizer_single.merge(
        lines, LocalizedLines(lines=(LocalizedLine(id=line_id, text_vi=text),)), {line_id}
    )


def build_escalation_agent(
    base: Any,
    deps: LocalizerDeps,
    context: LocalizerContext,
    lines: LocalizedLines,
    arm: Arm,
) -> Agent[LocalizerDeps, LocalizedLines]:
    def settings_for(ctx: Any) -> Any:
        return answer_only_settings(len(ctx.deps.expected_ids()))

    agent: Agent[LocalizerDeps, LocalizedLines] = Agent(
        base,
        name="localizer_escalate",
        deps_type=LocalizerDeps,
        output_type=LocalizedLines,
        instructions=ESCALATE_INSTRUCTIONS,
        retries={"output": 1},
        capabilities=[CodeMode()],
        model_settings=settings_for,
    )

    async def check(candidates: dict[int, list[str]]) -> dict[int, list[str]]:
        """For each segment id, tells whether each candidate rewrite passes: "ok", or what is
        wrong with it. Candidates are checked against the same slot and glossary as the draft."""
        arm.check_calls += 1
        out: dict[int, list[str]] = {}
        for raw_id, texts in candidates.items():
            line_id = int(raw_id)
            verdicts = []
            for text in texts:
                arm.candidates_checked += 1
                found = localizer_single.assess(
                    deps, context, _replace(lines, line_id, text), check_terms=True
                ).problems.get(line_id)
                verdicts.append("ok" if found is None else found.note)
            out[line_id] = verdicts
        return out

    agent.tool_plain(check)
    return agent


async def run_item(
    item: Item, agents: tuple[Any, Any], base: Any, repeat_label: str
) -> Result:
    answer, repair = agents
    result = Result(f"{item.name}{repeat_label}")
    began = time.perf_counter()
    try:
        first = await answer.run(
            localizer_single.render_prompt(item.deps, item.context),
            deps=item.deps,
            usage_limits=localizer_single.SINGLE_USAGE_LIMITS,
        )
    except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
        result.notes.append(f"draft gave up: {exc}"[:200])
        return result
    result.draft_seconds = time.perf_counter() - began
    lines = first.output
    assessment = localizer_single.assess(item.deps, item.context, lines, check_terms=True)
    result.problems_before = len(assessment.problems)
    if not assessment.problems:
        return result
    asked = set(assessment.problems)

    arm = Arm()
    first_after = None
    began = time.perf_counter()
    try:
        repaired = await repair.run(
            localizer_single.render_repair_prompt(item.deps, item.context, assessment),
            deps=item.deps,
            usage_limits=localizer_single.SINGLE_USAGE_LIMITS,
        )
        merged = localizer_single.merge(lines, repaired.output, asked)
        first_after = localizer_single.assess(item.deps, item.context, merged, check_terms=True)
        arm.requests = repaired.usage.requests
        arm.problems_after, arm.unusable_after = len(first_after.problems), len(first_after.unusable())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
        arm.error = str(exc)[:200]
        arm.problems_after = result.problems_before  # a run that gave up fixed nothing
    arm.seconds = time.perf_counter() - began
    result.repair = arm

    # Control: the same repair request once more for the lines still broken. If this closes
    # the gap, the gain is the extra round, not the agent.
    second = Arm(seconds=arm.seconds, requests=arm.requests, problems_after=arm.problems_after,
                 unusable_after=arm.unusable_after, error=arm.error)
    if first_after is not None and first_after.problems:
        began = time.perf_counter()
        try:
            again = await repair.run(
                localizer_single.render_repair_prompt(item.deps, item.context, first_after),
                deps=item.deps,
                usage_limits=localizer_single.SINGLE_USAGE_LIMITS,
            )
            final = localizer_single.assess(
                item.deps,
                item.context,
                localizer_single.merge(merged, again.output, set(first_after.problems)),
                check_terms=True,
            )
            second.requests += again.usage.requests
            second.problems_after, second.unusable_after = len(final.problems), len(final.unusable())
        except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
            second.error = str(exc)[:200]
        second.seconds += time.perf_counter() - began
    result.repair2 = second

    arm = Arm()
    agent = build_escalation_agent(base, item.deps, item.context, lines, arm)
    began = time.perf_counter()
    try:
        escalated = await agent.run(
            localizer_single.render_repair_prompt(item.deps, item.context, assessment),
            deps=item.deps,
            usage_limits=UsageLimits(request_limit=5),
        )
        after = localizer_single.assess(
            item.deps, item.context, localizer_single.merge(lines, escalated.output, asked), check_terms=True
        )
        arm.requests = escalated.usage.requests
        arm.problems_after, arm.unusable_after = len(after.problems), len(after.unusable())
    except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
        arm.error = str(exc)[:200]
        arm.problems_after = result.problems_before  # a run that gave up fixed nothing
    arm.seconds = time.perf_counter() - began
    result.escalate = arm
    return result


def _mean(values: list[float]) -> float:
    return round(statistics.fmean(values), 2) if values else float("nan")


def summarise(results: list[Result]) -> dict:
    drafted = [r for r in results if r.draft_seconds]
    failing = [r for r in drafted if r.problems_before]
    both = [r for r in failing if r.repair and r.escalate]
    rep = [r.repair for r in both]
    rep2 = [r.repair2 for r in both]
    esc = [r.escalate for r in both]
    escalation_rate = len(failing) / len(drafted) if drafted else 0.0
    return {
        "drafts": len(drafted),
        "drafts_with_problems": len(failing),
        "escalation_rate": round(escalation_rate, 3),
        "problems_before_total": sum(r.problems_before for r in failing),
        "repair": {
            "problems_left": sum(a.problems_after for a in rep),
            "lines_still_unusable": sum(a.unusable_after for a in rep),
            "mean_seconds": _mean([a.seconds for a in rep]),
            "mean_requests": _mean([a.requests for a in rep]),
            "errors": sum(1 for a in rep if a.error),
        },
        "repair_twice": {
            "problems_left": sum(a.problems_after for a in rep2),
            "mean_seconds": _mean([a.seconds for a in rep2]),
            "mean_requests": _mean([a.requests for a in rep2]),
            "errors": sum(1 for a in rep2 if a.error),
        },
        "escalate": {
            "problems_left": sum(a.problems_after for a in esc),
            "lines_still_unusable": sum(a.unusable_after for a in esc),
            "mean_seconds": _mean([a.seconds for a in esc]),
            "mean_requests": _mean([a.requests for a in esc]),
            "mean_check_calls": _mean([a.check_calls for a in esc]),
            "errors": sum(1 for a in esc if a.error),
        },
        "mean_draft_seconds": _mean([r.draft_seconds for r in drafted]),
        "mean_extra_seconds_per_video_if_escalating": round(
            escalation_rate * (_mean([a.seconds for a in esc]) - _mean([a.seconds for a in rep])), 2
        )
        if both
        else None,
    }


async def main_async(args: argparse.Namespace) -> int:
    load_env(localizer_model=args.model)
    settings = Settings()
    http = openrouter_client()
    agents = _agents(settings, True, http)
    base = AnswerOnlyModel(model_for("localizer", settings, http), reasoning_off=True)
    items = load_items(args.tight)
    jobs = [(item, f"#{n + 1}" if args.repeat > 1 else "") for item in items for n in range(args.repeat)]
    if args.limit:
        jobs = jobs[: args.limit]
    gate = asyncio.Semaphore(args.concurrency)

    async def one(item: Item, label: str) -> Result:
        async with gate:
            result = await run_item(item, agents, base, label)
            flag = "" if not result.problems_before else (
                f" problems {result.problems_before} -> repair {result.repair.problems_after if result.repair else '?'}"
                f" / escalate {result.escalate.problems_after if result.escalate else '?'}"
            )
            print(f"  {result.item}: draft {result.draft_seconds:.1f}s{flag}", flush=True)
            return result

    print(f"{len(jobs)} draft(s), tight={args.tight}, model={settings.localizer_model}")
    results = await asyncio.gather(*(one(i, label) for i, label in jobs))
    summary = summarise(list(results))
    summary["tight"] = args.tight
    summary["model"] = settings.localizer_model
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"summary": summary, "items": [r.__dict__ | {k: getattr(r, k).__dict__ if getattr(r, k) else None for k in ("repair", "repair2", "escalate")} for r in results]},
                indent=2,
                default=str,
            )
        )
    await http.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", help="OpenRouter model id; defaults to LOCALIZER_MODEL")
    parser.add_argument("--repeat", type=int, default=1, help="drafts per item")
    parser.add_argument("--tight", type=float, default=1.0, help="scale every slot by this")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="only the first N drafts")
    parser.add_argument("--out", help="write the per-item results here as JSON")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

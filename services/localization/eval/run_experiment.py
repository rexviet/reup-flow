"""Runs the Localizer over a Langfuse dataset and scores every answer: one dataset run.

    uv run python eval/run_experiment.py --model deepseek/deepseek-v4-pro \
        --prompt-version localizer-2 [--dataset localizer-v1] [--reasoning off|default]

The Localizer is the production one-request flow (`localizer_single.localize`: one
answer, at most one repair, the same prompt, checks and token cap), run as a plain agent
outside Temporal. Each item is one trace on Langfuse: a span per model request (with its
tokens and the cost OpenRouter reports), the lines it returned, and the scores of
`evaluators` on it; the trace is linked to the item under the run's name, so two runs
(two models, two prompt versions) are compared side by side in the dataset's view.

`--prompt-version` names the prompt the code holds now: changing the prompt means
changing the code, and this label is how the two runs are told apart.
"""

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import evaluators
from common import LANGFUSE, Langfuse, load_env
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from localization.agents import localizer_single
from localization.agents.answer_only import AnswerOnlyModel, answer_only_settings
from localization.agents.localizer import LocalizerDeps
from localization.agents.localizer_single import LocalizerContext
from localization.config import Settings
from localization.observability.tracing import init_tracing
from localization.providers.llm import model_for, openrouter_client
from localization.storage.object_store import work_key

_tracer = trace.get_tracer("localization.eval")


class SpanPerRequest(WrapperModel):
    """One span per model request, so the cost the response hook puts on the current
    span lands on that request's own span (as the model-request activity's span does
    in production)."""

    def __init__(self, wrapped: Any, name: str) -> None:
        super().__init__(wrapped)
        self._name = name

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        with _tracer.start_as_current_span(self._name):
            return await self.wrapped.request(messages, model_settings, model_request_parameters)


def _deps(item_input: dict, run_name: str) -> LocalizerDeps:
    aweme = item_input["aweme_id"]
    return LocalizerDeps(
        aweme_id=aweme,
        workflow_id=f"eval-{run_name}"[:256],
        channel_id=item_input["channel_id"],
        # Never read: the context below is what the keys would have been read into.
        transcript_key=work_key(aweme, 0, "transcript.json"),
        subtitle_key=work_key(aweme, 0, "subtitles.json"),
        syllables_per_sec=item_input["syllables_per_sec"],
        segment_count=item_input["segment_count"],
    )


def _agents(settings: Settings, reasoning_off: bool, http) -> tuple[Any, Any]:  # noqa: ANN001
    def settings_for(ctx) -> ModelSettings:  # noqa: ANN001
        return answer_only_settings(len(ctx.deps.expected_ids()))

    base = AnswerOnlyModel(model_for("localizer", settings, http), reasoning_off=reasoning_off)
    answer = localizer_single.build_single_agent(
        SpanPerRequest(base, "localizer.answer"),
        name=localizer_single.SINGLE_NAME,
        instructions=localizer_single.INSTRUCTIONS,
        model_settings=settings_for,
    )
    repair = localizer_single.build_single_agent(
        SpanPerRequest(base, "localizer.repair"),
        name=localizer_single.REPAIR_NAME,
        instructions=localizer_single.REPAIR_INSTRUCTIONS,
        model_settings=settings_for,
    )
    return answer, repair


async def run_item(
    item: dict, run_name: str, agents: tuple[Any, Any], judges: dict[str, Any]
) -> tuple[str, list[evaluators.Result], dict[int, str]]:
    item_input = item["input"]
    deps = _deps(item_input, run_name)
    context = LocalizerContext.model_validate(item_input["context"])
    with _tracer.start_as_current_span(f"localizer-eval {item_input['aweme_id']}") as root:
        root.set_attribute("session.id", run_name)
        root.set_attribute("langfuse.trace.input", json.dumps(item_input["context"]))
        trace_id = format(root.get_span_context().trace_id, "032x")
        results: list[evaluators.Result] = []
        lines: dict[int, str] = {}
        try:
            assessment = await localizer_single.localize(*agents, deps, context, check_terms=True)
            lines = dict(assessment.texts)
            unusable = assessment.unusable()
            results.append(evaluators.Result("unusable_lines", float(len(unusable))))
            # A synthetic case says what a good answer must meet; a dataset item made
            # from a real run carries the run's own lines instead.
            expected = item.get("expectedOutput") or {}
            results += evaluators.expected_results(expected, context, lines, len(unusable))
        except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
            results.append(evaluators.Result("unusable_lines", None, f"gave up: {exc}"[:500]))
        root.set_attribute("langfuse.trace.output", json.dumps(lines, ensure_ascii=False))
        if lines:
            results += evaluators.code_results(context, lines, deps.syllables_per_sec)
            for name, agent in judges.items():
                with _tracer.start_as_current_span(f"judge.{name}"):
                    try:
                        results.append(await evaluators.judge(name, agent, context, lines))
                    except (UnexpectedModelBehavior, UsageLimitExceeded) as exc:
                        results.append(evaluators.Result(name, None, f"judge failed: {exc}"))
    return trace_id, results, lines


def rescore(langfuse: Langfuse, item: dict, trace_id: str) -> None:
    trace = langfuse.trace(trace_id) or {}
    output = trace.get("output")
    lines = {
        int(k): v
        for k, v in (json.loads(output) if isinstance(output, str) else output or {}).items()
    }
    unusable = next(
        (int(s["value"]) for s in trace.get("scores", []) if s["name"] == "unusable_lines"), 0
    )
    context = LocalizerContext.model_validate(item["input"]["context"])
    expected = item.get("expectedOutput") or {}
    for result in evaluators.expected_results(expected, context, lines, unusable):
        langfuse.score(trace_id, result.name, result.value or 0.0, result.comment)


async def main_async(args: argparse.Namespace) -> int:
    load_env(langfuse_host=args.langfuse, localizer_model=args.model, judge_model=args.judge_model)
    settings = Settings()
    provider = init_tracing(settings, "localization-eval")
    if not isinstance(provider, TracerProvider):
        print("Langfuse is not configured (LANGFUSE_HOST / keys): nothing would be recorded")
        return 2
    langfuse = Langfuse.from_env(args.langfuse)
    run_name = args.run_name or (
        f"{args.prompt_version} {settings.localizer_model} reasoning-{args.reasoning} "
        f"{time.strftime('%y%m%d-%H%M')}"
    )
    http = openrouter_client()
    agents = _agents(settings, args.reasoning == "off", http)
    judge_model = model_for("judge", settings, http)
    judges = {
        "fidelity": evaluators.build_judge(
            SpanPerRequest(judge_model, "judge.request"), evaluators.FIDELITY_RUBRIC
        ),
        "naturalness": evaluators.build_judge(
            SpanPerRequest(judge_model, "judge.request"), evaluators.NATURALNESS_RUBRIC
        ),
    }
    metadata = {
        "model": settings.localizer_model,
        "prompt_version": args.prompt_version,
        "reasoning": args.reasoning,
        "judge_model": settings.judge_model,
    }
    items = list(langfuse.items(args.dataset))
    # A run given by name continues: an item it already holds is not asked again (its
    # lines are on its trace); only its expected checks are judged again, at no cost, so
    # a fixed check reaches the earlier items too.
    done = langfuse.run_items(args.dataset, run_name)
    print(f"run '{run_name}': {len(items)} item(s), {len(done)} already done")
    summary: dict[str, list[float]] = {}
    for item in items:
        if item["id"] in done:
            rescore(langfuse, item, done[item["id"]])
            continue
        trace_id, results, lines = await run_item(item, run_name, agents, judges)
        provider.force_flush()
        langfuse.link_run_item(run_name, item["id"], trace_id, json.dumps(metadata), metadata)
        for result in results:
            if result.value is not None:
                langfuse.score(trace_id, result.name, result.value, result.comment)
                summary.setdefault(result.name, []).append(result.value)
        shown = ", ".join(
            f"{r.name}={r.value:.2f}" if r.value is not None else f"{r.name}=n/a" for r in results
        )
        print(f"  {item['input']['aweme_id']}: {len(lines)} line(s); {shown}")
    for name, values in summary.items():
        print(f"mean {name}: {sum(values) / len(values):.3f} over {len(values)}")
    provider.shutdown()
    await http.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", required=True, help="OpenRouter model id")
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument("--dataset", default="localizer-v1")
    parser.add_argument("--reasoning", choices=("off", "default"), default="off")
    parser.add_argument("--judge-model", help="defaults to JUDGE_MODEL")
    parser.add_argument("--run-name")
    parser.add_argument("--langfuse", default=LANGFUSE)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

"""Stand-ins for what the Localizer stage reaches, built to the real shapes.

`FakePorts` is the tools' view of a video: transcript segments, subtitle spans and
a glossary that follows the real rules (`check_term`, first writer wins). The
scripted model is a `FunctionModel` that calls the tools a script names and then
answers, so the agent, its validator and its durable execution all run for real.
"""

import re
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio import activity

from localization.activities.glossary import MAX_WRITES_PER_RUN, check_term
from localization.agents.localizer import (
    SaveResult,
    SegmentView,
    TermView,
    bind_ports,
)
from localization.agents.temporal import bind_model_factory
from localization.contracts import (
    LocalizationInput,
    LocalizedLines,
    LocalizedScriptRef,
    ScriptResult,
)
from localization.contracts.voice import VoiceRef
from localization.storage.object_store import work_key

DEFAULT_SEGMENTS = (
    SegmentView(0, 0, 1500, "大家好，欢迎来到我的频道"),
    SegmentView(1, 1800, 3000, "今天我们来看看这个新手机和苹果"),
)


@dataclass
class FakePorts:
    segment_list: Sequence[SegmentView] = DEFAULT_SEGMENTS
    spans: Sequence[tuple[int, int]] = ()
    glossary: dict[tuple[str, str], str] = field(default_factory=dict)
    saved_by: dict[str, int] = field(default_factory=Counter)
    calls: Counter = field(default_factory=Counter)

    async def segments(self, transcript_key: str) -> Sequence[SegmentView]:
        self.calls["segments"] += 1
        return self.segment_list

    async def on_screen_spans(self, subtitle_key: str) -> Sequence[tuple[int, int]]:
        self.calls["on_screen_spans"] += 1
        return self.spans

    async def lookup_terms(self, channel_id: str, terms: list[str]) -> Sequence[TermView]:
        self.calls["lookup_terms"] += 1
        return [
            TermView(zh, vi, None)
            for (channel, zh), vi in self.glossary.items()
            if channel == channel_id and (not terms or zh in terms)
        ]

    async def save_term(
        self,
        *,
        channel_id: str,
        term_zh: str,
        term_vi: str,
        note: str | None,
        aweme_id: str,
        workflow_id: str,
    ) -> SaveResult:
        self.calls["save_term"] += 1
        reason = check_term(term_zh, term_vi, note)
        if reason is not None:
            return SaveResult("rejected", f"not saved: {reason}")
        if (channel_id, term_zh) in self.glossary:
            return SaveResult("exists", "not changed", self.glossary[(channel_id, term_zh)])
        if self.saved_by[workflow_id] >= MAX_WRITES_PER_RUN:
            return SaveResult("limit", "not saved: limit")
        self.glossary[(channel_id, term_zh)] = term_vi
        self.saved_by[workflow_id] += 1
        return SaveResult("inserted", "saved")


Answer = Callable[[list[int]], list[dict[str, Any]]]


def translate_all(ids: list[int]) -> list[dict[str, Any]]:
    return [{"id": i, "text_vi": f"bản dịch số {i}"} for i in ids]


def _ids_from_prompt(messages: list[ModelMessage]) -> list[int]:
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    match = re.search(r"ids are 0 to (-?\d+)", part.content)
                    if match:
                        return list(range(int(match.group(1)) + 1))
    raise AssertionError("the prompt does not name the ids")


@dataclass
class ScriptedModel:
    """Calls the tools in `tool_calls` (one model turn each), then answers with
    `answers[n]` on its n-th answer attempt (the last one repeats)."""

    tool_calls: Sequence[tuple[str, dict[str, Any]]] = (("read_transcript", {}),)
    answers: Sequence[Answer] = (translate_all,)
    requests: int = 0
    answer_attempts: int = 0
    seen_messages: list[list[ModelMessage]] = field(default_factory=list)

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests += 1
        self.seen_messages.append(messages)
        called = sum(
            isinstance(part, ToolCallPart) and part.tool_name != info.output_tools[0].name
            for m in messages
            if isinstance(m, ModelResponse)
            for part in m.parts
        )
        if called < len(self.tool_calls):
            name, args = self.tool_calls[called]
            return ModelResponse(parts=[ToolCallPart(name, args)])
        answer = self.answers[min(self.answer_attempts, len(self.answers) - 1)]
        self.answer_attempts += 1
        payload = {"lines": answer(_ids_from_prompt(messages))}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    def model(self) -> FunctionModel:
        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return self._respond(messages, info)

        return FunctionModel(respond, model_name="scripted-localizer")


@dataclass
class LocalizerFakes:
    """`ports` is a `FakePorts` or the real `StorePorts`. `assemble` is the real
    activity when a test wants the script written to a (moto) object store; without
    it a stand-in with the same signature and result shape runs."""

    ports: Any = field(default_factory=FakePorts)
    model: ScriptedModel = field(default_factory=ScriptedModel)
    rate: float = 4.0
    assemble: Callable | None = None
    calls: Counter = field(default_factory=Counter)
    # (round, version) of each `assemble_script` call.
    assembled: list[tuple[int, int]] = field(default_factory=list)
    # Segments the first translation is flagged for (a contact its source lacks).
    flagged_ids: tuple[int, ...] = ()

    def activities(self) -> list[Callable]:
        fakes = self

        @activity.defn(name="voice_rate")
        async def voice_rate(voice: VoiceRef) -> float:
            fakes.calls["voice_rate"] += 1
            return fakes.rate

        @activity.defn(name="assemble_script")
        async def assemble_script(
            spec: LocalizationInput,
            transcript_key: str,
            subtitle_key: str,
            lines: LocalizedLines,
            round: int = 0,
            version: int = 1,
        ) -> ScriptResult:
            fakes.calls["assemble_script"] += 1
            fakes.assembled.append((round, version))
            flagged = fakes.flagged_ids if round == 0 else ()
            return ScriptResult(
                script=LocalizedScriptRef(
                    script_key=work_key(spec.aweme_id, round, f"script.v{version}.json"),
                    version=version,
                ),
                needs_human=bool(flagged),
                flagged_segment_ids=tuple(flagged),
            )

        return [voice_rate, self.assemble or assemble_script]


@contextmanager
def bound_localizer(fakes: LocalizerFakes) -> Iterator[None]:
    """The agent's process-wide bindings, set for a test and always undone."""
    bind_ports(fakes.ports)
    model = fakes.model.model()
    bind_model_factory(lambda: model)
    try:
        yield
    finally:
        bind_ports(None)
        bind_model_factory(None)

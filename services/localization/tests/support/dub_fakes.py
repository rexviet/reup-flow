"""Stand-ins for the dubbing stage, registered under the real activity names.

`DubFakes` is the whole stage without audio: it answers with the shapes the real
activities return, and it can say that some segments do not fit, which sends the run
through the Dubber (a real agent on a scripted model, with fake ports that take the
time a real TTS call takes, so queue overlap can be measured).
"""

import asyncio
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio import activity

from localization.agents import dubber as dubber_module
from localization.agents.dubber import DubberDeps, SegmentFit, SpokenLine
from localization.agents.temporal import DUBBER_MODEL_ALIAS, bind_model_factory
from localization.contracts import (
    DubbedAudio,
    DubbedScriptRef,
    DubDecisions,
    FitReport,
    IngestResult,
    LocalizationInput,
    LocalizedScriptRef,
    SynthClip,
    SynthesisPlan,
    VoiceProfile,
)
from localization.contracts.voice import VoiceRef
from localization.storage.object_store import ArtifactMissingError, work_key

SHA = "a" * 64


@dataclass
class FakeDubberPorts:
    """What the Dubber's tools reach. `speak` holds for `speak_seconds` and records
    when it ran, so a test can measure how many ran at once."""

    segment_slot_ms: int = 1000
    duration_ms: int = 1800
    speak_seconds: float = 0.0
    calls: Counter = field(default_factory=Counter)
    spans: list[tuple[float, float]] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)

    async def segment(self, deps: DubberDeps, segment_id: int) -> SegmentFit:
        self.calls["segment"] += 1
        return SegmentFit(
            segment_id,
            "你好",
            f"câu dài số {segment_id}",
            self.segment_slot_ms,
            self.duration_ms,
        )

    async def speak(self, deps: DubberDeps, text_vi: str) -> SpokenLine:
        self.calls["speak"] += 1
        entered = time.monotonic()
        if self.speak_seconds:
            await asyncio.sleep(self.speak_seconds)
        self.spans.append((entered, time.monotonic()))
        self.spoken.append(text_vi)
        return SpokenLine(duration_ms=900)


def _pending(messages: list[ModelMessage]) -> list[int]:
    import re

    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    match = re.search(r"segment ids \[([0-9, ]*)\]", content)
                    if match:
                        return [int(x) for x in match.group(1).split(",") if x.strip()]
    raise AssertionError("the prompt does not name the segment ids")


@dataclass
class ScriptedDubber:
    """Speaks each pending segment (all in one model turn, so the tool calls run
    side by side), then returns one decision per segment."""

    rewrites_per_segment: int = 1
    stretch: float = 1.1
    requests: int = 0

    def model(self) -> FunctionModel:
        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.requests += 1
            ids = _pending(messages)
            answered = sum(
                isinstance(p, ToolCallPart) and p.tool_name == "synthesize"
                for m in messages
                if isinstance(m, ModelResponse)
                for p in m.parts
            )
            if answered < self.rewrites_per_segment * len(ids):
                round_no = answered // max(len(ids), 1)
                return ModelResponse(
                    parts=[
                        ToolCallPart("synthesize", {"id": i, "text_vi": f"câu ngắn {i}.{round_no}"})
                        for i in ids
                    ]
                )
            # The final text is the last one it spoke, as a real Dubber's would be.
            last = self.rewrites_per_segment - 1
            payload = {
                "items": [
                    {
                        "id": i,
                        "text_vi": f"câu ngắn {i}.{last}" if last >= 0 else f"câu ngắn {i}",
                        "stretch_ratio": self.stretch,
                    }
                    for i in ids
                ]
            }
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

        return FunctionModel(respond, model_name="scripted-dubber")


@dataclass
class DubFakes:
    unfit_ids: tuple[int, ...] = ()
    engine: str | None = "engine-a"
    # `tools`: the tool loop on `dubber`. `single`: one request on the bound model.
    mode: str = "tools"
    hashes: int = 3
    ports: FakeDubberPorts = field(default_factory=FakeDubberPorts)
    dubber: ScriptedDubber = field(default_factory=ScriptedDubber)
    calls: Counter = field(default_factory=Counter)
    # (activity, nth call) -> the stage whose artifact that call finds missing.
    missing_on: dict[tuple[str, int], str] = field(default_factory=dict)
    # (activity, nth call) -> an error that call raises instead of answering.
    fail_on: dict[tuple[str, int], Exception] = field(default_factory=dict)
    # hash -> the stage whose artifact is found missing, once, by the first batch that
    # carries that hash (so a test can pick which batch fails, whatever the order).
    missing_hash: dict[str, str] = field(default_factory=dict)
    # The hashes each `synthesize_segments` call was given.
    batches: list[list[str]] = field(default_factory=list)
    # Seconds each `synthesize_segments` call takes.
    batch_seconds: float = 0.0
    # What `write_dubbed_script` was given, one entry per call.
    decisions_seen: list[DubDecisions] = field(default_factory=list)
    # The round each `write_dubbed_script` call was given, and what each `commit_dub`
    # call was given (round, decisions).
    rounds_seen: list[int] = field(default_factory=list)
    committed: list[tuple[int, DubDecisions]] = field(default_factory=list)
    # Lines whose rewrite `write_dubbed_script` says it refused.
    refused_ids: tuple[int, ...] = ()

    def _enter(self, name: str, key: str = "work/7001/r0/x") -> None:
        self.calls[name] += 1
        stage = self.missing_on.get((name, self.calls[name]))
        if stage is not None:
            raise ArtifactMissingError(key, stage)
        error = self.fail_on.get((name, self.calls[name]))
        if error is not None:
            raise error

    def activities(self) -> list[Callable]:
        fakes = self

        @activity.defn(name="voice_profile")
        async def voice_profile(voice: VoiceRef) -> VoiceProfile:
            fakes._enter("voice_profile")
            return VoiceProfile(engine=fakes.engine)

        @activity.defn(name="plan_synthesis")
        async def plan_synthesis(
            spec: LocalizationInput, script_key: str, engine: str | None
        ) -> SynthesisPlan:
            fakes._enter("plan_synthesis")
            return SynthesisPlan(hashes=tuple(f"{i:064x}" for i in range(fakes.hashes)))

        @activity.defn(name="synthesize_segments")
        async def synthesize_segments(
            spec: LocalizationInput, engine: str | None, script_key: str, hashes: list[str]
        ) -> list[SynthClip]:
            fakes._enter("synthesize_segments")
            fakes.batches.append(list(hashes))
            for h in hashes:
                stage = fakes.missing_hash.pop(h, None)
                if stage is not None:
                    raise ArtifactMissingError("work/7001/r0/x", stage)
            if fakes.batch_seconds:
                await asyncio.sleep(fakes.batch_seconds)
            return [SynthClip(hash=h, key=f"cache/tts/{h}.wav", duration_ms=500) for h in hashes]

        @activity.defn(name="measure_fit")
        async def measure_fit(
            spec: LocalizationInput, script_key: str, engine: str | None
        ) -> FitReport:
            fakes._enter("measure_fit")
            return FitReport(unfit_ids=fakes.unfit_ids)

        @activity.defn(name="write_dubbed_script")
        async def write_dubbed_script(
            spec: LocalizationInput,
            script: LocalizedScriptRef,
            decisions: DubDecisions,
            round: int = 0,
        ) -> DubbedScriptRef:
            fakes._enter("write_dubbed_script")
            fakes.decisions_seen.append(decisions)
            fakes.rounds_seen.append(round)
            return DubbedScriptRef(
                script_key=work_key(spec.aweme_id, round, f"script.v{script.version + 1}.json"),
                version=script.version + 1,
                refused_ids=fakes.refused_ids,
            )

        @activity.defn(name="commit_dub")
        async def commit_dub(
            spec: LocalizationInput,
            script: LocalizedScriptRef,
            engine: str | None,
            decisions: DubDecisions,
            round: int = 0,
        ) -> str:
            fakes._enter("commit_dub")
            fakes.committed.append((round, decisions))
            return work_key(spec.aweme_id, round, "dub-segments.json")

        @activity.defn(name="assemble_audio")
        async def assemble_audio(
            spec: LocalizationInput,
            script: LocalizedScriptRef,
            segments_key: str,
            ingest: IngestResult,
            round: int,
        ) -> DubbedAudio:
            fakes._enter("assemble_audio")
            return DubbedAudio(
                audio_key=work_key(spec.aweme_id, round, "dubbed.wav"),
                audio_sha256=SHA,
                script_key=script.script_key,
                script_version=script.version,
                segments_key=segments_key,
            )

        return [
            voice_profile,
            plan_synthesis,
            synthesize_segments,
            measure_fit,
            write_dubbed_script,
            commit_dub,
            assemble_audio,
        ]

    @contextmanager
    def bound(self) -> Iterator[None]:
        """The Dubber's process-wide bindings, set for a test and always undone."""
        dubber_module.bind_ports(self.ports)
        model = self.dubber.model()
        bind_model_factory(lambda: model, DUBBER_MODEL_ALIAS)
        try:
            yield
        finally:
            dubber_module.bind_ports(None)
            bind_model_factory(None, DUBBER_MODEL_ALIAS)


def by_name(activities: Sequence[Callable]) -> dict[str, Callable]:
    return {a.__temporal_activity_definition.name: a for a in activities}  # type: ignore[attr-defined]

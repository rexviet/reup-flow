"""The Vbee dubbing workflow on a local Temporal server, with the real activities wherever
the voice, the script or the clips are touched, and stand-ins only for what needs a real
video (separation, subtitle detection, silence detection, mixing, layout, render).

The voice is a fake Vbee that reads real WAV audio at a set rate of syllables a second (the
sample sentence at its own rate, and lines with a slow word slower), so the rate the
workflow measures on the clips and the overruns the review finds come from real clip
lengths, through the real synthesizer, cache and dub document. The shortener agents are
the real durable agents on a scripted model. Publishing is the real activity.
"""

import asyncio
import re
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256

from pydantic import SecretStr
from pydantic_ai.durable_exec.temporal import AgentPlugin
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle

from localization.activities.dub_script import DubScriptActivities
from localization.activities.review_render import _verdict
from localization.activities.script_document import read_script
from localization.activities.slot_script import SlotScriptActivity
from localization.activities.synthesize import SynthesizeActivities
from localization.activities.vbee_stt import VbeeSttActivity
from localization.activities.voice_rate import SAMPLE_TEXT, VoiceRateActivity
from localization.agents import review_checks
from localization.agents.reviewer import Loudness
from localization.agents.temporal import (
    MODEL_ALIAS,
    bind_model_factory,
    slot_shortener_agent,
    slot_shortener_repair_agent,
)
from localization.contracts import (
    DubbedAudio,
    DubSegmentsDocument,
    EditSpecRef,
    IngestResult,
    LocalizationInput,
    LocalizedScriptRef,
    ReviewVerdict,
    SubtitleDocument,
    SubtitleTrack,
)
from localization.contracts.vbee_flow import (
    VBEE_CONFIGURED_VOICE,
    Slot,
    SlotPlan,
    SpeechTranscript,
    Utterance,
    VbeeDubbingResult,
)
from localization.contracts.voice import VbeeSettings, VoiceRef
from localization.providers.tts_router import ConfiguredVoices, TtsByProvider
from localization.storage.object_store import work_key
from localization.text.vi_syllables import count_syllables
from localization.workers.run import QueueSpec, build_worker
from localization.workflows.vbee_dubbing import VbeeDubbingWorkflow
from tests.support.dub_fakes import by_name
from tests.support.inputs import make_input
from tests.support.localizer_fakes import prompt_text
from tests.support.prep_worker import running
from tests.support.review_fakes import ReviewFakes
from tests.support.tts_fakes import make_wav

VIDEO_MS = 20_000
TARGET_LUFS = -14.0
# The voice id the workers are configured with. A test plants a canary here.
VOICE_ID = "vbee-voice-under-test"
# A word that the fake voice reads slowly wherever it is in a line.
SLOW_WORD = "chậm"


def vbee_input(aweme_id: str = "7001") -> LocalizationInput:
    voice = VoiceRef(provider="vbee", voice_id=VBEE_CONFIGURED_VOICE, settings=VbeeSettings())
    return make_input(aweme_id).model_copy(update={"voice": voice})


def words(count: int, word: str = "một") -> str:
    """A line of `count` one-syllable words, without punctuation: read in one piece."""
    return " ".join([word] * count)


def three_slots(*texts: str) -> SlotPlan:
    """Slots of 4000 ms, one every 5000 ms."""
    return SlotPlan(
        slots=tuple(
            Slot(id=i, start_ms=i * 5000, end_ms=i * 5000 + 4000, text=t)
            for i, t in enumerate(texts)
        ),
        has_speech=bool(texts),
    )


@dataclass
class FakeVbeeVoice:
    """Reads a line at `rate` syllables a second, a line with one of the words of `slow` in
    it at that word's rate, and the sample sentence at `sample_rate`. Records the voice id
    each call came with."""

    rate: float = 6.0
    sample_rate: float = 6.0
    slow: dict[str, float] = field(default_factory=lambda: {SLOW_WORD: 2.5})
    calls: Counter = field(default_factory=Counter)
    voices: list[str] = field(default_factory=list)

    def seconds(self, text: str) -> float:
        if text == SAMPLE_TEXT:
            rate = self.sample_rate
        else:
            rate = min([self.rate, *(r for w, r in self.slow.items() if w in text.split())])
        return count_syllables(text) / rate

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        self.calls[text] += 1
        self.voices.append(voice.voice_id)
        return make_wav(self.seconds(text))


@dataclass
class FakeVbeeStt:
    answer: SpeechTranscript | Exception = field(
        default_factory=lambda: SpeechTranscript(
            utterances=(Utterance(text="xin chào", start_ms=0, end_ms=14_000),), audio_ms=VIDEO_MS
        )
    )
    calls: int = 0

    async def transcribe(self, wav: bytes) -> SpeechTranscript:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class MemoryRates:
    """`localization.voice_rates`, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, float] = {}

    async def get(self, key: str) -> float | None:
        return self.rows.get(key)

    async def put(self, key: str, rate: float) -> float:
        return self.rows.setdefault(key, rate)


_ASKED = re.compile(r"^\[(\d+)\] slot \d+ ms, up to (\d+) syllables", re.MULTILINE)


@dataclass
class ScriptedShortener:
    """Answers every line it is asked for with one syllable under its budget, in `word`
    (one the voice reads at its normal rate, unless a test says otherwise)."""

    word: str = "gọn"
    prompts: list[str] = field(default_factory=list)

    @staticmethod
    def asked(prompt: str) -> dict[int, int]:
        """Slot id -> syllable budget, for each line a prompt asks for."""
        return {int(i): int(b) for i, b in _ASKED.findall(prompt)}

    def model(self) -> FunctionModel:
        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            prompt = prompt_text(messages)
            self.prompts.append(prompt)
            lines = [
                {"id": i, "text": words(max(1, budget - 1), self.word)}
                for i, budget in self.asked(prompt).items()
            ]
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"lines": lines})])

        return FunctionModel(respond, model_name="scripted-shortener")


@dataclass
class VbeeRun:
    """One run of the workflow and everything a test wants to look at after it."""

    slots: SlotPlan = field(default_factory=lambda: three_slots(words(10), words(10), words(10)))
    voice: FakeVbeeVoice = field(default_factory=FakeVbeeVoice)
    stt: FakeVbeeStt = field(default_factory=FakeVbeeStt)
    shortener: ScriptedShortener = field(default_factory=ScriptedShortener)
    reviewing: ReviewFakes = field(default_factory=ReviewFakes)
    voice_id: str = VOICE_ID
    # The voice the run is started with.
    spec: LocalizationInput = field(default_factory=vbee_input)
    rates: MemoryRates = field(default_factory=MemoryRates)
    calls: Counter = field(default_factory=Counter)
    # What each `plan_slots` call was given.
    planned_from: list[tuple[str, SpeechTranscript]] = field(default_factory=list)
    # The dub document each review read.
    reviewed: list[DubSegmentsDocument] = field(default_factory=list)
    handle: WorkflowHandle | None = None
    outcome: VbeeDubbingResult | BaseException | None = None

    @property
    def store(self):  # noqa: ANN201
        return self.reviewing.store

    @property
    def result(self) -> VbeeDubbingResult:
        assert isinstance(self.outcome, VbeeDubbingResult), self.outcome
        return self.outcome

    def row(self) -> dict:
        return self.reviewing.runs.rows[(self.spec.aweme_id, self.spec.generation)]

    async def scheduled(self) -> list[str]:
        assert self.handle is not None
        history = await self.handle.fetch_history()
        return [
            e.activity_task_scheduled_event_attributes.activity_type.name
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        ]

    async def queues(self) -> dict[str, str]:
        assert self.handle is not None
        history = await self.handle.fetch_history()
        return {
            e.activity_task_scheduled_event_attributes.activity_type.name: (
                e.activity_task_scheduled_event_attributes.task_queue.name
            )
            for e in history.events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        }

    def stand_ins(self) -> list[Callable]:
        run = self
        store = self.store

        def enter(name: str) -> None:
            run.calls[name] += 1

        @activity.defn(name="ingest")
        async def ingest(spec: LocalizationInput) -> IngestResult:
            enter("ingest")
            vocals = work_key(spec.aweme_id, 0, "vocals.wav")
            store.put_bytes(vocals, make_wav(0.2))
            return IngestResult(
                vocals_key=vocals,
                bgm_key=work_key(spec.aweme_id, 0, "bgm.wav"),
                duration_ms=VIDEO_MS,
                width=1080,
                height=1920,
                fps=30,
            )

        @activity.defn(name="detect_subtitles")
        async def detect_subtitles(spec: LocalizationInput) -> SubtitleTrack:
            enter("detect_subtitles")
            key = work_key(spec.aweme_id, 0, "subtitles.json")
            doc = SubtitleDocument(width=1080, height=1920, region=None, cues=())
            store.put_bytes(key, doc.model_dump_json().encode())
            return SubtitleTrack(subtitle_key=key, region=None)

        @activity.defn(name="plan_slots")
        async def plan_slots(
            spec: LocalizationInput, vocals_key: str, transcript: SpeechTranscript
        ) -> SlotPlan:
            enter("plan_slots")
            run.planned_from.append((vocals_key, transcript))
            return run.slots

        @activity.defn(name="assemble_audio")
        async def assemble_audio(
            spec: LocalizationInput,
            script: LocalizedScriptRef,
            segments_key: str,
            ingest: IngestResult,
            round: int,
        ) -> DubbedAudio:
            enter("assemble_audio")
            doc = store.get_bytes(segments_key, produced_by="dub_script")
            return DubbedAudio(
                audio_key=work_key(spec.aweme_id, round, "dubbed.wav"),
                audio_sha256=sha256(doc).hexdigest(),
                script_key=script.script_key,
                script_version=script.version,
                segments_key=segments_key,
            )

        @activity.defn(name="review_dub")
        async def review_dub(
            spec: LocalizationInput,
            script: LocalizedScriptRef,
            dub: DubbedAudio,
            spec_ref: EditSpecRef,
            round: int,
        ) -> ReviewVerdict:
            """The real checks of the lines and their timing, on the real script and dub
            document; the track is taken to be at its loudness target."""
            enter("review_dub")
            doc = read_script(store, script.script_key)
            segments = DubSegmentsDocument.model_validate_json(
                store.get_bytes(dub.segments_key, produced_by="dub_script")
            )
            run.reviewed.append(segments)
            found = review_checks.line_findings(doc.segments)
            found += review_checks.timing_findings(
                doc.segments,
                segments,
                VIDEO_MS,
                Loudness(TARGET_LUFS, -3.0),
                target_lufs=TARGET_LUFS,
                true_peak_dbtp=-1.5,
            )
            return _verdict(found)

        return [ingest, detect_subtitles, plan_slots, assemble_audio, review_dub]

    @asynccontextmanager
    async def workers(self, client: Client) -> AsyncIterator[None]:
        store = self.store
        tts = TtsByProvider(None, self.voice)
        resolve = ConfiguredVoices(SecretStr(self.voice_id)).resolve
        synthesis = SynthesizeActivities(store, tts, model=None, resolve=resolve)
        dub = DubScriptActivities(store, synthesis.synthesizer)
        rate = VoiceRateActivity(tts, self.rates, model=None, resolve=resolve)  # type: ignore[arg-type]
        hear = VbeeSttActivity(store, self.stt)
        fake = by_name(self.stand_ins())
        review = by_name(self.reviewing.activities())
        agents = [AgentPlugin(a) for a in (slot_shortener_agent, slot_shortener_repair_agent)]
        model = self.shortener.model()
        bind_model_factory(lambda: model, MODEL_ALIAS)
        try:
            async with running(
                [
                    build_worker(client, QueueSpec("workflows", workflows=[VbeeDubbingWorkflow])),
                    build_worker(
                        client,
                        QueueSpec(
                            "media",
                            activities=[
                                fake["ingest"],
                                fake["assemble_audio"],
                                review["layout_captions"],
                            ],  # type: ignore[list-item]
                        ),
                    ),
                    build_worker(client, QueueSpec("stt", activities=[hear.vbee_stt])),
                    build_worker(
                        client,
                        QueueSpec(
                            "io",
                            activities=[
                                fake["detect_subtitles"],
                                fake["plan_slots"],
                                fake["review_dub"],
                                SlotScriptActivity(store).write_slot_script,
                                synthesis.plan_synthesis,
                                dub.clip_durations,
                                dub.commit_dub,
                                review["publish"],
                            ],  # type: ignore[list-item]
                        ),
                    ),
                    build_worker(client, QueueSpec("llm", plugins=agents)),
                    build_worker(client, QueueSpec("render", activities=[review["render"]])),  # type: ignore[list-item]
                    build_worker(
                        client,
                        QueueSpec(
                            "tts", 2, activities=[rate.voice_rate, synthesis.synthesize_segments]
                        ),
                    ),
                ]
            ):
                yield
        finally:
            bind_model_factory(None, MODEL_ALIAS)


async def run_vbee(client: Client, run: VbeeRun, *, timeout: float = 60) -> VbeeRun:
    async with run.workers(client):
        run.handle = await client.start_workflow(
            "VbeeDubbingWorkflow",
            run.spec,
            id=f"video-{run.spec.aweme_id}-g{run.spec.generation}-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=VbeeDubbingResult,
        )
        try:
            run.outcome = await asyncio.wait_for(run.handle.result(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            run.outcome = exc
    return run

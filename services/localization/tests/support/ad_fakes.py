"""The ad check of a run: the REAL `classify_video` activity over an object store on a
directory, with Jev a stand-in that answers what the test says and keeps what it was asked.

The preparation fakes name a transcript but write none, so the stand-in activity writes the
transcript of the test (`lines`) where the run's transcript key says, then hands over to the
real one: what Jev is shown is what the real activity built from that document.

The texts are short excerpts of real speech-to-text answers: `AD_LINES` from a video that
sells an iron supplement (Jev scored it 0.99), `CHANNEL_LINES` from one that asks viewers to
like, share and tip a channel (0.01).
"""

import dataclasses
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from temporalio import activity

from localization.activities.classify_video import ClassifyVideoActivity
from localization.contracts import LocalizationInput, SttSegment, Transcript, TranscriptDocument
from localization.providers.jev import JevResult
from localization.storage.object_store import ObjectStore
from tests.support.file_s3 import FileS3

AD_LINES = (
    "对比无数款我一直回购的就是这个无垢女博士附铁软糖",
    "温和好吸收还不刺激小小一颗就有4.6安纳铁每天吃三颗刚好满足一天所需",
    "现在品牌还有活动",
)
CHANNEL_LINES = ("请不吝点赞订阅", "转发", "打赏支持明镜与点点栏目")


@dataclasses.dataclass
class FakeJev:
    """Answers every question with `score`, or raises `fail`; keeps each state it was shown."""

    score: float = 0.02
    fail: Exception | None = None
    asked: list[tuple[dict, list[str]]] = dataclasses.field(default_factory=list)

    async def ask(self, state, questions, on_usage=None):  # noqa: ANN001, ANN201
        self.asked.append((state, list(questions)))
        if self.fail is not None:
            raise self.fail
        return JevResult(
            model="typesafe/jev-1.13-20260917",
            answers={name: self.score for name in questions},
            input_tokens=200,
            output_tokens=10,
            latency_ms=1100,
        )


def transcript_document(lines: Sequence[str]) -> TranscriptDocument:
    return TranscriptDocument(
        language="zh",
        duration_ms=3000,
        segments=tuple(
            SttSegment(id=i, start_ms=i * 1000, end_ms=i * 1000 + 900, text=text)
            for i, text in enumerate(lines)
        ),
        words=(),
    )


@dataclasses.dataclass
class AdFakes:
    # What the video says; by default a channel's request, which sells nothing.
    lines: Sequence[str] = CHANNEL_LINES
    jev: FakeJev = dataclasses.field(default_factory=FakeJev)
    enabled: bool = True
    threshold: float = 0.8
    calls: int = 0
    root: Path = dataclasses.field(
        default_factory=lambda: Path(tempfile.mkdtemp(prefix="ad-fakes-"))
    )

    def __post_init__(self) -> None:
        self.store = ObjectStore(FileS3(self.root), "reup-media")  # type: ignore[arg-type]

    def activities(self) -> list[Callable]:
        fakes = self
        real = ClassifyVideoActivity(
            self.store,
            lambda: fakes.jev,  # type: ignore[arg-type,return-value]
            threshold=self.threshold,
            enabled=self.enabled,
        )

        @activity.defn(name="classify_video")
        async def classify_video(spec: LocalizationInput, transcript: Transcript) -> bool:
            fakes.calls += 1
            fakes.store.put_bytes(
                transcript.transcript_key,
                transcript_document(fakes.lines).model_dump_json().encode(),
                content_type="application/json",
            )
            return await real.classify_video(spec, transcript)

        return [classify_video]

"""The review activity with real video: frames cut by ffmpeg from the rendered mp4,
loudness measured on its audio, the OCR asked about real pixels. The model is a
scripted one; the OCR provider is a stand-in that records what it was asked."""

import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.review_render import FRAME_WIDTH, ReviewRenderActivity
from localization.contracts import (
    BBox,
    Cover,
    DubbedAudio,
    DubSegment,
    DubSegmentsDocument,
    EditSpec,
    EditSpecRef,
    LocalizedScript,
    LocalizedScriptRef,
    LocalizedSegment,
    RenderResult,
)
from localization.storage.object_store import ObjectStore, work_key
from tests.support.file_s3 import FileS3
from tests.support.inputs import make_input

WIDTH, HEIGHT, SECONDS = 270, 480, 3
SPEC = make_input()
SCRIPT_KEY = work_key("7001", 1, "script.v3.json")
SEGMENTS_KEY = work_key("7001", 1, "dub-segments.json")
SPEC_KEY = work_key("7001", 1, "edit-spec.json")
RENDER_KEY = "work/7001/render-" + "c" * 64 + ".mp4"
COVER = BBox(x=20, y=380, w=230, h=60)

PASS = {"pass": True, "scores": {"fidelity": 0.9, "timing": 0.9, "visual": 0.9}, "issues": []}


def make_video(path: Path, *, width: int = WIDTH, height: int = HEIGHT) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            str(SECONDS),
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],  # fmt: skip
        check=True,
    )


@dataclass
class Ocr:
    box: BBox | None = None
    # Raised instead of answering (an answer that is not a box).
    fail: Exception | None = None
    asked: list[tuple[int, int, bytes]] = field(default_factory=list)

    async def detect_region(self, frame_png: bytes, width: int, height: int) -> BBox | None:
        self.asked.append((width, height, frame_png))
        if self.fail is not None:
            raise self.fail
        return self.box


@dataclass
class Scripted:
    calls: Sequence[tuple[str, dict[str, Any]]]
    answer: dict[str, Any] = field(default_factory=lambda: PASS)
    seen: list[list[ModelMessage]] = field(default_factory=list)

    def model(self) -> FunctionModel:
        async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            self.seen.append(messages)
            made = sum(
                isinstance(p, ToolCallPart) and p.tool_name != info.output_tools[0].name
                for m in messages
                if isinstance(m, ModelResponse)
                for p in m.parts
            )
            if made < len(self.calls):
                name, args = self.calls[made]
                return ModelResponse(parts=[ToolCallPart(name, args)])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, self.answer)])

        return FunctionModel(respond, model_name="scripted-reviewer")


@pytest.fixture
def store(tmp_path: Path) -> ObjectStore:
    store = ObjectStore(FileS3(tmp_path / "bucket"), "reup-media")
    script = LocalizedScript(
        aweme_id="7001",
        version=3,
        prompt_version="p",
        segments=tuple(
            LocalizedSegment(
                id=i,
                start_ms=i * 1000,
                end_ms=i * 1000 + 900,
                slot_ms=900,
                src_zh="你好",
                text_vi=f"xin chào {i}",
                on_screen=False,
            )
            for i in range(3)
        ),
    )
    store.put_bytes(SCRIPT_KEY, script.model_dump_json().encode())
    segments = DubSegmentsDocument(
        aweme_id="7001",
        script_version=3,
        segments=tuple(
            DubSegment(
                id=i,
                text_vi=f"xin chào {i}",
                tts_key=f"cache/tts/{i:064x}.wav",
                duration_ms=800,
                stretch_ratio=1.0,
                overflow_unresolved=False,
            )
            for i in range(3)
        ),
    )
    store.put_bytes(SEGMENTS_KEY, segments.model_dump_json().encode())
    spec = EditSpec(
        source_key="work/7001/r0/source.mp4",
        audio_key="work/7001/r1/dubbed.wav",
        audio_sha256="a" * 64,
        script_version=3,
        width=WIDTH,
        height=HEIGHT,
        fps=10,
        duration_ms=SECONDS * 1000,
        cover=Cover(region=COVER, color="#101010", opacity_pct=92),
        caption_box=COVER,
        captions=(),
        font="f.ttf",
        font_sha256="b" * 64,
    )
    store.put_bytes(SPEC_KEY, spec.model_dump_json().encode())
    video = tmp_path / "render.mp4"
    make_video(video)
    store.put_bytes(RENDER_KEY, video.read_bytes())
    return store


def activity(store: ObjectStore, scripted: Scripted, ocr: Ocr | None = None):
    model = scripted.model()
    return ReviewRenderActivity(
        store, ocr or Ocr(), lambda: model, loudness_lufs=-14.0, true_peak_dbtp=-1.5
    )


async def review(act: ReviewRenderActivity):
    return await ActivityEnvironment().run(
        act.review_render,
        SPEC,
        RenderResult(render_key=RENDER_KEY, spec_sha256="c" * 64),
        LocalizedScriptRef(script_key=SCRIPT_KEY, version=3),
        DubbedAudio(
            audio_key="work/7001/r1/dubbed.wav",
            audio_sha256="a" * 64,
            script_key=SCRIPT_KEY,
            script_version=3,
            segments_key=SEGMENTS_KEY,
        ),
        EditSpecRef(
            spec_key=SPEC_KEY, spec_sha256="c" * 64, script_version=3, audio_sha256="a" * 64
        ),
        1,
    )


def images_in(messages: list[ModelMessage]) -> list[BinaryContent]:
    return [
        c
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
        for c in (part.content if isinstance(part.content, list) else [])
        if isinstance(c, BinaryContent)
    ]


def tool_text(messages: list[ModelMessage]) -> str:
    return "\n".join(
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if part.part_kind == "tool-return"
    )


# --- real frames ------------------------------------------------------------------------


async def test_eight_real_frames_are_cut_from_the_video_and_shown_to_the_model(
    store: ObjectStore,
) -> None:
    times = [i * 350 for i in range(8)]
    scripted = Scripted(calls=[("sample_frames", {"times_ms": times})])
    verdict = await review(activity(store, scripted))
    assert verdict.passed is True
    images = images_in(scripted.seen[-1])
    assert len(images) == 8
    # Real JPEGs, all different (the test pattern moves), no larger than a model needs.
    assert all(i.data.startswith(b"\xff\xd8") for i in images)
    assert len({i.data for i in images}) == 8
    assert all(len(i.data) < 200_000 for i in images)


async def test_frames_are_scaled_down_to_what_a_model_needs(store: ObjectStore) -> None:
    # The video is narrower than the limit, so it is left at its own width ...
    scripted = Scripted(calls=[("sample_frames", {"times_ms": [0]})])
    await review(activity(store, scripted))
    probe = images_in(scripted.seen[-1])[0]
    assert _jpeg_width(probe.data) == WIDTH
    assert WIDTH < FRAME_WIDTH


def _jpeg_width(data: bytes) -> int:
    with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
        f.write(data)
        f.flush()
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width", "-of", "csv=p=0", f.name],
            capture_output=True, check=True, text=True,
        )  # fmt: skip
    return int(out.stdout.strip())


async def test_a_wide_video_is_scaled_down_to_the_frame_width(
    tmp_path: Path, store: ObjectStore
) -> None:
    wide = tmp_path / "wide.mp4"
    make_video(wide, width=1920, height=1080)
    store.put_bytes(RENDER_KEY, wide.read_bytes())
    big = EditSpec.model_validate_json(store.get_bytes(SPEC_KEY, produced_by="t")).model_copy(
        update={"width": 1920, "height": 1080, "cover": None}
    )
    store.put_bytes(SPEC_KEY, big.model_dump_json().encode())
    scripted = Scripted(calls=[("sample_frames", {"times_ms": [0]})])
    await review(activity(store, scripted))
    assert _jpeg_width(images_in(scripted.seen[-1])[0].data) == FRAME_WIDTH


# --- the other tools on real media ------------------------------------------------------


async def test_the_alignment_report_carries_the_loudness_of_the_real_audio(
    store: ObjectStore,
) -> None:
    scripted = Scripted(calls=[("alignment_report", {})])
    await review(activity(store, scripted))
    report = tool_text(scripted.seen[-1])
    assert "LUFS" in report and "dBTP" in report
    # A -ve number measured from the sine in the video, not a made-up one.
    assert "Integrated loudness -" in report


async def test_the_ocr_is_asked_about_a_frame_at_the_videos_own_size(store: ObjectStore) -> None:
    ocr = Ocr(box=BBox(x=30, y=390, w=200, h=40))
    scripted = Scripted(calls=[("residual_source_text", {"times_ms": [500, 1500]})])
    await review(activity(store, scripted, ocr))
    assert [(w, h) for w, h, _ in ocr.asked] == [(WIDTH, HEIGHT)] * 2
    assert all(frame.startswith(b"\xff\xd8") for _, _, frame in ocr.asked)
    report = tool_text(scripted.seen[-1])
    assert report.count("inside the place of the Vietnamese caption") == 2


async def test_text_the_ocr_finds_outside_the_cover_is_reported_as_residue(
    store: ObjectStore,
) -> None:
    ocr = Ocr(box=BBox(x=10, y=40, w=200, h=40))
    scripted = Scripted(calls=[("residual_source_text", {"times_ms": [500]})])
    await review(activity(store, scripted, ocr))
    assert "OUTSIDE the place of the Vietnamese caption" in tool_text(scripted.seen[-1])


async def test_an_ocr_that_cannot_read_a_frame_does_not_end_the_review(
    store: ObjectStore,
) -> None:
    from localization.providers.errors import UnusableAnswer

    ocr = Ocr(fail=UnusableAnswer("ocr: the reply is not the requested JSON"))
    scripted = Scripted(calls=[("residual_source_text", {"times_ms": [500, 1500]})])
    verdict = await review(activity(store, scripted, ocr))
    assert verdict.passed is True
    assert tool_text(scripted.seen[-1]).count("could not check this frame") == 2


async def test_an_ocr_that_is_broken_still_fails_the_review(store: ObjectStore) -> None:
    """Only a malformed answer is tolerated per frame; a rejected key or a down provider
    is not "could not check this frame"."""
    from localization.providers.errors import PermanentProviderError

    ocr = Ocr(fail=PermanentProviderError("ocr: HTTP 401"))
    scripted = Scripted(calls=[("residual_source_text", {"times_ms": [500]})])
    with pytest.raises(Exception, match="401"):
        await review(activity(store, scripted, ocr))


# --- what it leaves behind and how it fails ---------------------------------------------


def review_dirs() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("review-*")}


async def test_the_local_copy_of_the_video_is_removed_when_the_review_ends(
    store: ObjectStore,
) -> None:
    before = review_dirs()
    await review(activity(store, Scripted(calls=[("sample_frames", {"times_ms": [0]})])))
    assert review_dirs() == before


async def test_the_local_copy_is_removed_when_the_review_fails_too(store: ObjectStore) -> None:
    before = review_dirs()
    bad = Scripted(calls=[("read_script", {})], answer={"pass": True})  # not a verdict
    with pytest.raises(Exception):  # noqa: B017,PT011
        await review(activity(store, bad))
    assert review_dirs() == before


async def test_a_reviewer_that_never_settles_fails_for_good(store: ObjectStore) -> None:
    endless = Scripted(calls=[("read_script", {})] * 40)
    with pytest.raises(ApplicationError) as info:
        await review(activity(store, endless))
    assert (info.value.type, info.value.non_retryable) == ("ReviewRunaway", True)


async def test_a_render_that_is_gone_is_a_lost_artifact(store: ObjectStore) -> None:
    gone = ObjectStore(FileS3(Path(tempfile.mkdtemp())), "reup-media")
    for key in (SCRIPT_KEY, SEGMENTS_KEY, SPEC_KEY):
        gone.put_bytes(key, store.get_bytes(key, produced_by="t"))
    with pytest.raises(ApplicationError) as info:
        await review(activity(gone, Scripted(calls=[])))
    assert info.value.type == "ArtifactMissingError"


async def test_the_verdict_is_all_that_comes_back(store: ObjectStore) -> None:
    verdict = await review(activity(store, Scripted(calls=[("sample_frames", {"times_ms": [0]})])))
    assert len(verdict.model_dump_json()) < 2_000

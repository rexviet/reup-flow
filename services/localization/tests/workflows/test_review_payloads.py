"""What the review leaves in a workflow's history. The Reviewer looks at real frames
inside its activity; none of them, and nothing large, may reach Temporal."""

import asyncio
import base64
import json
import uuid
from pathlib import Path

from temporalio.client import Client

from localization.activities.review_render import ReviewRenderActivity
from localization.contracts import (
    DubSegment,
    DubSegmentsDocument,
    EditSpec,
    LocalizationResult,
    LocalizedScript,
    LocalizedSegment,
)
from localization.contracts.limits import MAX_PAYLOAD_BYTES
from localization.storage.object_store import work_key
from tests.activities.test_review_render import PASS, Ocr, Scripted, make_video
from tests.support.dub_fakes import DubFakes
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.review_fakes import ReviewFakes

# 5 of every 6 lines of a page of base64 are what a PNG or JPEG starts with.
PNG_BASE64 = "iVBOR"
JPEG_BASE64 = "/9j/"


def decoded_payloads(history: dict) -> list[str]:
    """Every payload in the history, decoded: the JSON export wraps each in base64, so
    a search of the raw text would not see an image inside one."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, str) and "metadata" in node:
                found.append(base64.b64decode(data).decode("utf-8", "replace"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(history)
    return found


def put_documents(reviewing: ReviewFakes, video: Path) -> None:
    store = reviewing.store
    script = LocalizedScript(
        aweme_id="7001",
        version=2,
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
            for i in range(2)
        ),
    )
    store.put_bytes(work_key("7001", 0, "script.v2.json"), script.model_dump_json().encode())
    segments = DubSegmentsDocument(
        aweme_id="7001",
        script_version=2,
        segments=tuple(
            DubSegment(
                id=i,
                text_vi=f"xin chào {i}",
                tts_key=f"cache/tts/{i:064x}.wav",
                duration_ms=800,
                stretch_ratio=1.0,
                overflow_unresolved=False,
            )
            for i in range(2)
        ),
    )
    store.put_bytes(work_key("7001", 0, "dub-segments.json"), segments.model_dump_json().encode())
    spec = EditSpec(
        source_key="work/7001/r0/source.mp4",
        audio_key="work/7001/r0/dubbed.wav",
        audio_sha256="a" * 64,
        script_version=2,
        width=270,
        height=480,
        fps=10,
        duration_ms=3000,
        cover=None,
        caption_box={"x": 0, "y": 300, "w": 270, "h": 100},  # type: ignore[arg-type]
        captions=(),
        font="f.ttf",
        font_sha256="b" * 64,
    )
    store.put_bytes(work_key("7001", 0, "edit-spec.json"), spec.model_dump_json().encode())


async def test_eight_real_frames_never_reach_the_history(
    temporal_client: Client, tmp_path: Path
) -> None:
    video = tmp_path / "render.mp4"
    make_video(video)
    scripted = Scripted(
        calls=[
            ("sample_frames", {"times_ms": [i * 350 for i in range(8)]}),
            ("residual_source_text", {"times_ms": [100, 2000]}),
        ],
        answer=PASS,
    )
    reviewing = ReviewFakes(render_bytes=video.read_bytes())
    model = scripted.model()
    reviewing.real_review = ReviewRenderActivity(
        reviewing.store, Ocr(), lambda: model, loudness_lufs=-14.0, true_peak_dbtp=-1.5
    )
    put_documents(reviewing, video)
    async with prep_workers(
        temporal_client,
        PrepFakes(),
        LocalizerFakes(),
        dubbing=DubFakes(),
        reviewing=reviewing,
    ):
        handle = await temporal_client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        result = await asyncio.wait_for(handle.result(), timeout=60)
        history = json.loads((await handle.fetch_history()).to_json())
    assert result.publish is not None and result.publish.outcome == "published"

    # The control: the images were really there, in the model's own requests.
    from tests.activities.test_review_render import images_in

    assert len(images_in(scripted.seen[-1])) == 8
    # And nowhere in what Temporal stored.
    payloads = decoded_payloads(history)
    assert payloads, "no payload was decoded: the check below would prove nothing"
    assert max(len(p.encode()) for p in payloads) < MAX_PAYLOAD_BYTES
    for text in payloads:
        assert PNG_BASE64 not in text and JPEG_BASE64 not in text
    # The review's own result is a verdict and nothing else.
    assert any('"pass":true' in p.replace(" ", "") for p in payloads)


def test_the_helper_can_see_an_image_inside_a_payload() -> None:
    """The control for the check above: an image in a payload is found by decoding."""
    jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 40).decode()
    wrapped = base64.b64encode(json.dumps({"frame": jpeg}).encode()).decode()
    history = {"events": [{"input": {"payloads": [{"metadata": {}, "data": wrapped}]}}]}
    (text,) = decoded_payloads(history)
    assert JPEG_BASE64 in text
    assert JPEG_BASE64 not in json.dumps(history)  # a search of the raw history would miss it

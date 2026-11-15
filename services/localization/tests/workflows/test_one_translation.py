"""One translation, through a rerun: the text drawn on the video is the text that was
spoken. The dubbing (clips, cache, the Dubber's rewrite), the script documents and the
layout are the real activities over one object store; only the audio track, the render
and the review are stand-ins. This is what the plan's second acceptance criterion says:
the captions of the spec match the `text-vi` of the clips the track was built from, also
after a run that was sent back."""

import asyncio
import base64
import uuid

from temporalio.client import Client

from localization.activities.assemble_script import AssembleScriptActivity
from localization.activities.layout_captions import LayoutCaptionsActivity
from localization.activities.synthesize import META_TEXT
from localization.config import DEFAULT_CAPTION_FONT
from localization.contracts import (
    DubSegmentsDocument,
    EditSpec,
    LocalizationResult,
    SubtitleDocument,
    TranscriptDocument,
)
from localization.contracts.models import SttSegment
from localization.storage.object_store import ObjectStore, work_key
from tests.support.inputs import make_input
from tests.support.localizer_fakes import LocalizerFakes
from tests.support.prep_fakes import REGION, PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.real_dubbing import RealDubbing
from tests.support.review_fakes import ReviewFakes, issue, verdict
from tests.support.tts_fakes import FakeTts


def put_video(store: ObjectStore) -> None:
    """What the preparation stages (stand-ins here) would have left, in the real shape."""
    transcript = TranscriptDocument(
        language="zh",
        duration_ms=3000,
        segments=(
            SttSegment(id=0, start_ms=0, end_ms=1500, text="大家好，欢迎来到我的频道"),
            SttSegment(id=1, start_ms=1800, end_ms=3000, text="今天我们来看看这个新手机和苹果"),
        ),
        words=(),
    )
    store.put_bytes(work_key("7001", 0, "transcript.json"), transcript.model_dump_json().encode())
    subtitles = SubtitleDocument(width=1080, height=1920, region=REGION, cues=())
    store.put_bytes(work_key("7001", 0, "subtitles.json"), subtitles.model_dump_json().encode())
    store.put_bytes("source/7001.mp4", b"video-bytes")


def spoken(store: ObjectStore, key: str) -> str:
    """The text the TTS object was made from, as its metadata records it."""
    meta = store.metadata(key)
    assert meta is not None, key
    return base64.b64decode(meta[META_TEXT]).decode()


def words(text: str) -> str:
    return " ".join(text.split())


async def test_after_a_rerun_the_captions_are_the_text_that_was_spoken(
    temporal_client: Client,
) -> None:
    reviewing = ReviewFakes(
        verdicts=[verdict(issue("dub", "read too fast", segment_id=1)), verdict()]
    )
    store = reviewing.store
    put_video(store)
    tts = FakeTts()
    dubbing = RealDubbing(store, tts)
    reviewing.real_layout = LayoutCaptionsActivity(
        store, font_path=str(DEFAULT_CAPTION_FONT), cover_color="#101010", cover_opacity=0.92
    )
    localizing = LocalizerFakes(assemble=AssembleScriptActivity(store).assemble_script)
    async with prep_workers(
        temporal_client,
        PrepFakes(),
        localizing,
        dubbing=dubbing,
        reviewing=reviewing,
    ):
        handle = await temporal_client.start_workflow(
            "VideoLocalizationWorkflow",
            make_input(),
            id=f"video-7001-g1-{uuid.uuid4()}",
            task_queue="workflows",
            result_type=LocalizationResult,
        )
        result = await asyncio.wait_for(handle.result(), timeout=90)
    assert result.publish is not None and result.publish.outcome == "published"
    assert [x["round"] for x in reviewing.layouts] == [0, 1]

    for round_no in (0, 1):
        spec = EditSpec.model_validate_json(
            store.get_bytes(work_key("7001", round_no, "edit-spec.json"), produced_by="t")
        )
        doc = DubSegmentsDocument.model_validate_json(
            store.get_bytes(work_key("7001", round_no, "dub-segments.json"), produced_by="t")
        )
        # Every caption is the text of the clip its segment is played from.
        clips = {s.id: spoken(store, s.tts_key) for s in doc.segments}
        shown = {c.segment_id: words(c.text) for c in spec.captions}
        assert shown, "no caption was laid out: the comparison would prove nothing"
        for seg_id, text in shown.items():
            assert text == words(clips[seg_id]), (round_no, seg_id)
        assert spec.script_version == doc.script_version

    # The rerun changed what is said, and the second spec shows the new line, not the old.
    first = {
        c.segment_id: words(c.text)
        for c in EditSpec.model_validate_json(
            store.get_bytes(work_key("7001", 0, "edit-spec.json"), produced_by="t")
        ).captions
    }
    second = {
        c.segment_id: words(c.text)
        for c in EditSpec.model_validate_json(
            store.get_bytes(work_key("7001", 1, "edit-spec.json"), produced_by="t")
        ).captions
    }
    assert first[1] != second[1]
    assert first[0] == second[0]
    assert second[1].startswith("câu ngắn 1")

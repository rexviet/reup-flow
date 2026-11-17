"""The activity: the collection's images read and checked, the subtitle document updated."""

import hashlib
import shutil
import subprocess

import boto3
import cv2
import pytest
from moto import mock_aws
from temporalio.testing import ActivityEnvironment

from localization.activities.detect_watermarks import DetectWatermarksActivity
from localization.contracts import (
    BBox,
    CollectionSnapshot,
    SubtitleDocument,
    SubtitleTrack,
    WatermarkTemplate,
)
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore, work_key
from tests.support.inputs import make_input
from tests.watermarks.test_template_match import H, W, _draw, _scene, _template

BUCKET = "reup-media"
IMAGE_KEY = "source/collections/c/watermarks/w.png"
REGION = BBox(x=20, y=560, w=320, h=40)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture
def store():  # noqa: ANN201
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield ObjectStore(client, BUCKET)


def _setup(store: ObjectStore, tmp_path, sha: str | None = None):
    video = tmp_path / "source.mp4"
    encode = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray",
         "-s", f"{W}x{H}", "-r", "2", "-i", "-", "-pix_fmt", "yuv420p", "-c:v", "libx264",
         "-crf", "18", str(video)],
        stdin=subprocess.PIPE,
    )  # fmt: skip
    assert encode.stdin is not None
    for i in range(12):
        frame = _scene(i)
        _draw(frame, 16, 50)
        encode.stdin.write(frame.tobytes())
    encode.stdin.close()
    assert encode.wait() == 0
    spec = make_input()
    store.put_file(spec.video_key, video)
    png = cv2.imencode(".png", _template().image)[1].tobytes()
    store.put_bytes(IMAGE_KEY, png)
    image = WatermarkTemplate(
        key=IMAGE_KEY, sha256=sha or hashlib.sha256(png).hexdigest(), frame_height=H
    )
    spec = spec.model_copy(
        update={
            "collection": CollectionSnapshot(
                id="00000000-0000-4000-8000-000000000001", name="Kênh", watermarks=(image,)
            )
        }
    )
    key = work_key(spec.aweme_id, 0, "subtitles.json")
    document = SubtitleDocument(width=W, height=H, region=REGION, cues=())
    store.put_bytes(key, document.model_dump_json().encode())
    return spec, SubtitleTrack(subtitle_key=key, region=REGION), image


async def test_the_found_watermarks_go_into_the_document_and_the_track(store, tmp_path) -> None:
    spec, track, image = _setup(store, tmp_path)
    activity = DetectWatermarksActivity(store, tmp_root=tmp_path)
    result = await ActivityEnvironment().run(activity.detect_watermarks, spec, track)
    (mark,) = result.watermarks
    assert mark.region.x <= 16 and mark.end_ms == 6000
    assert result.region == REGION
    stored = SubtitleDocument.model_validate_json(
        store.get_bytes(track.subtitle_key, produced_by="detect_subtitles")
    )
    assert stored.watermarks == result.watermarks
    assert stored.watermark_templates == (image.sha256,)
    assert stored.region == REGION


async def test_an_image_whose_bytes_changed_is_refused(store, tmp_path) -> None:
    spec, track, _ = _setup(store, tmp_path, sha="b" * 64)
    activity = DetectWatermarksActivity(store, tmp_root=tmp_path)
    with pytest.raises(PermanentProviderError, match="bytes changed"):
        await ActivityEnvironment().run(activity.detect_watermarks, spec, track)


async def test_a_run_without_images_is_returned_as_it_came(store) -> None:
    track = SubtitleTrack(subtitle_key=work_key("7001", 0, "subtitles.json"), region=REGION)
    activity = DetectWatermarksActivity(store)
    assert await ActivityEnvironment().run(activity.detect_watermarks, make_input(), track) == track

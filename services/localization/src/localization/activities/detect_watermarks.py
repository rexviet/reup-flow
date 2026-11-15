"""Step 2c: the collection's watermark images found on the source, to be blurred.

Runs after `detect_subtitles` (a match inside the subtitle band is subtitle text) and
only for a run whose collection has images. What it finds is written into the subtitle
document, which the layout and the review project read the blur from.
"""

import asyncio
import hashlib
import tempfile
import threading
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import cv2
import numpy as np
from temporalio import activity

from localization.activities.ffmpeg import probe_video, run_blocking
from localization.activities.ingest import download_source
from localization.activities.persist import put_json
from localization.contracts import (
    LocalizationInput,
    SubtitleDocument,
    SubtitleTrack,
    Watermark,
    WatermarkTemplate,
)
from localization.observability.tracing import tag_session
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore, require_work_key, source_video_key
from localization.watermarks import template_match

# Who writes the images: a missing one is not made again by any stage of the run.
TEMPLATE_PRODUCER = "collection_watermarks"


def load_template(store: ObjectStore, template: WatermarkTemplate) -> template_match.Template:
    """The image, grey, after its bytes are checked against the sha256 the run was started
    with: an image swapped under the same key is refused, not matched."""
    data = store.get_bytes(template.key, produced_by=TEMPLATE_PRODUCER)
    if hashlib.sha256(data).hexdigest() != template.sha256:
        raise PermanentProviderError(f"watermark image {template.key}: its bytes changed")
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise PermanentProviderError(f"watermark image {template.key}: not a readable image")
    return template_match.Template(image=image, frame_height=template.frame_height)


def find_on_source(
    store: ObjectStore,
    source_key: str,
    templates: Sequence[WatermarkTemplate],
    document: SubtitleDocument,
    root: Path,
    cancel: threading.Event | None = None,
) -> tuple[Watermark, ...]:
    """Downloads the source and the images into `root` and searches them; shared with the
    review worker, which finds them again on a project already made."""
    loaded = [load_template(store, t) for t in templates]
    video = root / "source.mp4"
    download_source(store, source_key, video)
    info = probe_video(video)
    return template_match.find(
        video,
        loaded,
        document.region,
        width=info.width,
        height=info.height,
        duration_ms=info.duration_ms,
        cancel=cancel,
    )


class DetectWatermarksActivity:
    def __init__(self, store: ObjectStore, *, tmp_root: Path | None = None) -> None:
        self._store = store
        self._tmp_root = tmp_root

    @activity.defn(name="detect_watermarks")
    async def detect_watermarks(
        self, spec: LocalizationInput, subtitles: SubtitleTrack
    ) -> SubtitleTrack:
        tag_session(spec.aweme_id)
        templates = spec.collection.watermarks if spec.collection else ()
        if not templates:
            return subtitles
        require_work_key(spec, subtitles.subtitle_key)
        raw = await asyncio.to_thread(
            self._store.get_bytes, subtitles.subtitle_key, produced_by="detect_subtitles"
        )
        document = SubtitleDocument.model_validate_json(raw)
        cancel = threading.Event()
        with tempfile.TemporaryDirectory(prefix="marks-", dir=self._tmp_root) as tmp:
            found = await run_blocking(
                partial(
                    find_on_source,
                    self._store,
                    source_video_key(spec),
                    templates,
                    document,
                    Path(tmp),
                    cancel,
                ),
                cancel=cancel,
            )
        updated = document.model_copy(
            update={
                "watermarks": found,
                "watermark_templates": tuple(t.sha256 for t in templates),
            }
        )
        await asyncio.to_thread(put_json, self._store, subtitles.subtitle_key, updated)
        activity.logger.info("watermarks: %s found with %s image(s)", len(found), len(templates))
        return subtitles.model_copy(update={"watermarks": found})

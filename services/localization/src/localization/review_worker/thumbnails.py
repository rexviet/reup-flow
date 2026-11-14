"""A small still of every downloaded video, so each row of the dashboard list is a picture.

The thumbnail of video `<aweme>` is `output/thumbs/<aweme>.jpg`, cut from the source MP4 the
downloader stored; nothing in the database records it, and the dashboard derives the key from
the id alone. One video per pass, newest first (Douyin ids grow with time), so a large library
fills in over time instead of in one long run at start. A video whose thumbnail cannot be made
is remembered and not tried again until the worker restarts.

The source is untrusted input: it is copied to a private temporary file, must start like an
MP4 (`ftyp` at byte 4), and ffmpeg only ever reads that file (see `posters.cut_frame`).
"""

import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from localization.contracts.ids import validate_aweme_id, validate_source_key
from localization.review_worker import final_render
from localization.review_worker.posters import cut_frame, find_ffmpeg

log = logging.getLogger("review_worker.thumbnails")

THUMB_WIDTH = 320
THUMB_QUALITY = 5
FFMPEG_TIMEOUT_S = 20
# How long a pass that found nothing to do (or could not run) waits before the next one.
IDLE_S = 60
TEMP_PREFIX = "thumb-"

# Only the columns the `localization` role may read. Digit strings without leading zeros sort
# numerically by length, then text: newest first, without a cast that one bad row could break.
CANDIDATES_SQL = (
    "SELECT aweme_id, storage_path FROM videos WHERE storage_path IS NOT NULL"
    " ORDER BY length(aweme_id) DESC, aweme_id DESC"
)
S3_URL = re.compile(r"s3://(?P<bucket>[^/]+)/(?P<key>.+)")


def thumb_key(aweme_id: str) -> str:
    return f"output/thumbs/{validate_aweme_id(aweme_id)}.jpg"


def source_key(storage_path: str, bucket: str) -> str:
    """The source object of a `videos.storage_path`; ValueError unless it is a `source/` key
    of this bucket."""
    match = S3_URL.fullmatch(storage_path)
    if match is None or match["bucket"] != bucket:
        raise ValueError("storage_path is not a source object of this bucket")
    return validate_source_key(match["key"])


def looks_like_mp4(path: Path) -> bool:
    """An ISO base media file (MP4, MOV) starts with a box whose type is `ftyp`."""
    with path.open("rb") as fh:
        head = fh.read(8)
    return len(head) == 8 and head[4:8] == b"ftyp"


class Thumbnailer:
    """The state of the thumbnail passes of one worker process. One pass runs at a time, on the
    worker's thumbnail thread."""

    def __init__(
        self,
        store,
        work_dir: Path,
        *,
        min_free_gb: float,
        ffmpeg_bin: str | None = None,
        cut=cut_frame,
        clock=time.monotonic,
    ):
        self.store = store
        self.work_dir = work_dir
        self.min_free_gb = min_free_gb
        self.ffmpeg_bin = ffmpeg_bin
        self.cut = cut
        self.clock = clock
        self.done: set[str] = set()
        self.failed: set[str] = set()
        self.idle_until = float("-inf")
        self.cleaned = False
        self.warned: set[str] = set()

    def due(self) -> bool:
        return self.clock() >= self.idle_until

    def idle(self) -> None:
        self.idle_until = self.clock() + IDLE_S

    def warn_once(self, message: str, *args) -> None:
        """Conditions that hold for every video (no ffmpeg, a full disk, a refused query) are
        said once per start, not once a minute."""
        if message not in self.warned:
            self.warned.add(message)
            log.warning(message, *args)

    def run_pass(self, conn, stopping: threading.Event) -> str | None:
        """Makes the thumbnail of the newest video that has none; returns its id, or None when
        there was nothing to do (or it could not be made)."""
        self.work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.cleaned:
            self._clean_stale()
        ffmpeg = find_ffmpeg(self.ffmpeg_bin)
        if ffmpeg is None:
            self.warn_once("no ffmpeg found (set FFMPEG_BIN): no video thumbnails are made")
            self.idle()
            return None
        if not final_render.disk_ok(self.work_dir, self.min_free_gb)[0]:
            self.warn_once("video thumbnails paused: less than %g GB free", self.min_free_gb)
            self.idle()
            return None
        for row in conn.execute(CANDIDATES_SQL).fetchall():
            if stopping.is_set():
                return None
            aweme = row["aweme_id"]
            if aweme in self.done or aweme in self.failed:
                continue
            try:
                key = thumb_key(aweme)
                source = source_key(row["storage_path"], self.store.bucket)
            except ValueError:
                self.failed.add(aweme)
                log.warning("thumbnail of %s skipped: its storage path is not a source object",
                            aweme)  # fmt: skip
                continue
            if self.store.exists(key):
                self.done.add(aweme)
                continue
            if self._make(aweme, source, key, ffmpeg):
                self.done.add(aweme)
                return aweme
            self.failed.add(aweme)
            return None
        self.idle()
        return None

    def _make(self, aweme: str, source: str, key: str, ffmpeg: str) -> bool:
        started = time.monotonic()
        # mkstemp creates both files 0600, so no other user of the machine can read or swap them.
        fd, video_name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".mp4", dir=self.work_dir)
        out_fd, out_name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".jpg", dir=self.work_dir)
        os.close(out_fd)
        video, out = Path(video_name), Path(out_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                self.store.download_fileobj(source, fh, produced_by="downloader")
            if not looks_like_mp4(video):
                log.warning("thumbnail of %s not made: the source is not an MP4", aweme)
                return False
            if not self.cut(
                video, out, ffmpeg=ffmpeg, width=THUMB_WIDTH, quality=THUMB_QUALITY,
                timeout_s=FFMPEG_TIMEOUT_S,
            ):  # fmt: skip
                log.warning("thumbnail of %s not made: ffmpeg could not cut a frame", aweme)
                return False
            self.store.put_file(key, out, content_type="image/jpeg")
            log.info("thumbnail of %s made (%d KB) in %.1fs", aweme, out.stat().st_size // 1024,
                     time.monotonic() - started)  # fmt: skip
            return True
        except Exception as exc:  # noqa: BLE001
            # A storage error, a source that is gone, a full disk: this video only.
            log.warning("thumbnail of %s not made: %s", aweme, type(exc).__name__)
            return False
        finally:
            video.unlink(missing_ok=True)
            out.unlink(missing_ok=True)

    def _clean_stale(self) -> None:
        """Temporary files left by a worker that was killed mid-pass."""
        self.cleaned = True
        for path in self.work_dir.glob(f"{TEMP_PREFIX}*"):
            try:
                path.unlink()
            except OSError:
                pass

"""A still of a rendered video, so the dashboard can show each one as a picture.

The poster sits beside the video under the same name (`<revision>.mp4` becomes
`<revision>.jpg`), so nothing in the database says where it is: the dashboard derives the
key from the video's. Making one is best effort: a render is never failed or held back
because its picture could not be made.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from botocore.exceptions import ClientError

log = logging.getLogger("review_worker.posters")

POSTER_WIDTH = 480
FFMPEG_TIMEOUT_S = 120


def poster_key(final_key: str) -> str | None:
    """The key of the poster of the video at `final_key`; None for anything that is not an mp4."""
    return final_key[: -len(".mp4")] + ".jpg" if final_key.endswith(".mp4") else None


# Directories where ffmpeg is usually installed on a Mac, for a PATH that lost them (pm2 started
# from a bare shell falls back to /usr/bin:/bin).
FFMPEG_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
# The worker only ever reads local MP4 files: no network protocol, no other demuxer.
INPUT_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2"


def find_ffmpeg(configured: str | None = None) -> str | None:
    """`configured` (FFMPEG_BIN, an absolute path) when it is an executable file, else ffmpeg
    on PATH, else in the usual install directories; None when there is none."""
    if configured:
        path = Path(configured)
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
        log.warning("FFMPEG_BIN is not an executable file; looking for ffmpeg on PATH")
    found = shutil.which("ffmpeg")
    if found is None:
        found = shutil.which("ffmpeg", path=os.pathsep.join(FFMPEG_FALLBACK_DIRS))
    return found


def cut_frame(
    video: Path, out: Path, *, ffmpeg: str, width: int, quality: int, timeout_s: float
) -> bool:
    """One frame of the local file `video` as a JPEG `width` wide: a second in (the first
    frame is often black), else the first frame for a video shorter than that. ffmpeg reads
    only that file (no URL, no protocol but `file`, no demuxer but MP4's) and is killed after
    `timeout_s` per attempt."""
    for seek in ("1", "0"):
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    INPUT_FORMATS,
                    "-ss",
                    seek,
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    "-q:v",
                    str(quality),
                    str(out),
                ],  # fmt: skip
                check=True,
                capture_output=True,
                timeout=timeout_s,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.is_file() and out.stat().st_size > 0:
            return True
    return False


def make_poster(video: Path, out: Path) -> bool:
    """The poster of a rendered video, `POSTER_WIDTH` wide."""
    ffmpeg = find_ffmpeg(os.environ.get("FFMPEG_BIN"))
    if ffmpeg is None:
        log.warning("no ffmpeg found, no poster made")
        return False
    return cut_frame(
        video, out, ffmpeg=ffmpeg, width=POSTER_WIDTH, quality=4, timeout_s=FFMPEG_TIMEOUT_S
    )


DENIED_CODES = ("403", "AccessDenied")


def backfill(conn, store, stop: threading.Event) -> int:
    """Makes the poster of every rendered video that has none (renders from before posters
    existed). Returns how many it made. Slow: each one downloads its video. Needs to read
    `output/`: where the worker's policy only writes there, it stops at the first refusal and
    says so once, because the same refusal would come for every video."""
    rows = conn.execute(
        "SELECT final_key FROM localization.final_renders"
        " WHERE state = 'rendered' AND final_key IS NOT NULL ORDER BY rendered_at DESC"
    ).fetchall()
    made = 0
    for row in rows:
        if stop.is_set():
            break
        key = poster_key(row["final_key"])
        if key is None:
            continue
        with tempfile.TemporaryDirectory(prefix="poster-") as tmp:
            video, out = Path(tmp) / "final.mp4", Path(tmp) / "poster.jpg"
            try:
                if store.exists(key):
                    continue
                store.download_file(row["final_key"], video, produced_by="final_render")
                if make_poster(video, out):
                    store.put_file(key, out, content_type="image/jpeg")
                    made += 1
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", "unknown"))
                if code in DENIED_CODES:
                    log.warning(
                        "poster backfill stopped: the worker may not read output/ (%s); "
                        "videos rendered before posters existed keep no picture until they "
                        "are rendered again",
                        code,
                    )
                    break
                log.warning("poster backfill of one video failed: storage error %s", code)
            except Exception as exc:  # noqa: BLE001
                log.warning("poster backfill of one video failed: %s", type(exc).__name__)
    return made

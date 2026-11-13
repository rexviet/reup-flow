"""A still of a rendered video, so the dashboard can show each one as a picture.

The poster sits beside the video under the same name (`<revision>.mp4` becomes
`<revision>.jpg`), so nothing in the database says where it is: the dashboard derives the
key from the video's. Making one is best effort: a render is never failed or held back
because its picture could not be made.
"""

import logging
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


def make_poster(video: Path, out: Path) -> bool:
    """One frame, a second in (the first frame of a render is often black), at `POSTER_WIDTH`
    wide. A video shorter than a second gives its first frame."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        log.warning("no ffmpeg on PATH, no poster made")
        return False
    for seek in ("1", "0"):
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    seek,
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={POSTER_WIDTH}:-2",
                    "-q:v",
                    "4",
                    str(out),
                ],  # fmt: skip
                check=True,
                capture_output=True,
                timeout=FFMPEG_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.is_file() and out.stat().st_size > 0:
            return True
    return False


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

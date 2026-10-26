"""Step 1: probe the video and split the voice from the background music."""

import asyncio
import shutil
import tempfile
from pathlib import Path

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import extract_audio, probe_video, run_blocking
from localization.contracts import IngestResult, LocalizationInput
from localization.observability.tracing import tag_session
from localization.providers.base import StemSeparator
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore, work_key

# The round every preparation artifact belongs to; review rounds start after it.
PREP_ROUND = 0


class IngestActivity:
    def __init__(self, store: ObjectStore, separator: StemSeparator) -> None:
        self._store = store
        self._separator = separator

    @activity.defn(name="ingest")
    async def ingest(self, spec: LocalizationInput) -> IngestResult:
        tag_session(spec.aweme_id)
        with provider_errors(), tempfile.TemporaryDirectory(prefix="ingest-") as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            await run_blocking(download_source, self._store, spec.video_key, video)
            info = await run_blocking(probe_video, video)
            if not info.has_audio:
                raise PermanentProviderError("no_audio_track")
            audio = root / "audio.wav"
            await run_blocking(extract_audio, video, audio)
            stems_dir, vocals, background = await run_blocking(self._separator.separate, audio)
            try:
                for path in (vocals, background):
                    if path.suffix != ".wav":
                        raise PermanentProviderError("demucs: stems are not WAV files")
                vocals_key = work_key(spec.aweme_id, PREP_ROUND, "vocals.wav")
                bgm_key = work_key(spec.aweme_id, PREP_ROUND, "bgm.wav")
                await asyncio.to_thread(
                    self._store.put_file, vocals_key, vocals, content_type="audio/wav"
                )
                await asyncio.to_thread(
                    self._store.put_file, bgm_key, background, content_type="audio/wav"
                )
            finally:
                shutil.rmtree(stems_dir, ignore_errors=True)
        return IngestResult(
            vocals_key=vocals_key,
            bgm_key=bgm_key,
            duration_ms=info.duration_ms,
            width=info.width,
            height=info.height,
            fps=info.fps,
        )


def download_source(store: ObjectStore, key: str, path: Path) -> None:
    # The source video is produced by the downloader, which this workflow cannot
    # re-run: a missing one is a permanent failure the workflow will not retry.
    store.download_file(key, path, produced_by="download")

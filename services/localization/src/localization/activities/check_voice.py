"""Whether a video has a voice, measured on its source before anything else runs.

Many videos of a channel are music, or sounds, and nothing said. Preparing one costs the
most expensive step of a run (separating the voice from the music), and speech-to-text on
music makes words up that would then be translated and dubbed. So the source is checked
first: its audio is decoded to 16 kHz mono and a voice-activity detector says which part of
it is speech. Under the threshold (a share of the audio) the video has no voice, and the
workflow publishes the original instead of localizing it.

The detector is Silero VAD. WebRTC's VAD was measured too and calls 80 to 100% of a music
track speech; Silero measured every video of real spoken Chinese at 25% or more and the
music-only ones at 0.

A file without audio has no voice (a share of 0). A file without a video stream (sound alone,
or a still picture over it, which is what an image post with music is) is not measured: the
workflow skips it whatever its audio says, so the check stops at the probe and says only that.
A failure here is permanent (an unreadable
file, ffmpeg refusing it) or transient (the object store); the workflow localizes a video it
could not check, so the check never costs one.
"""

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol

from temporalio import activity

from localization.activities.errors import provider_errors
from localization.activities.ffmpeg import probe_video, run_blocking, run_ffmpeg
from localization.activities.ingest import download_source
from localization.contracts.models import LocalizationInput, VoiceCheck
from localization.observability.tracing import tag_session
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore

log = logging.getLogger(__name__)

# The detector's input: 16-bit little-endian mono PCM at this rate.
SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2
# Silero's own defaults, and the settings the threshold was chosen with: a chunk is speech
# at a probability over 0.5, and a stretch of speech shorter than 250 ms is not counted.
SILERO_THRESHOLD = 0.5
SILERO_MIN_SPEECH_MS = 250
# What `probe_video` says about a file whose only picture is cover art, or none at all.
_NO_VIDEO_STREAM = "no_video_stream"


class SpeechDetector(Protocol):
    def speech_seconds(self, pcm: bytes) -> float:
        """Seconds of speech in `pcm` (16 kHz mono, 16-bit little-endian)."""
        ...


class SileroSpeechDetector:
    """Silero VAD on the CPU. The model ships inside the `silero-vad` package (nothing is
    downloaded) and is loaded on first use, once per process. It keeps state from one chunk
    to the next, so one thread at a time uses it: a measure takes a second or two, and the
    downloads and decodes of other checks go on meanwhile."""

    def __init__(self) -> None:
        self._model: Any = None
        self._lock = threading.Lock()

    def speech_seconds(self, pcm: bytes) -> float:
        # Imported here: a process that never checks a voice (the media worker imports this
        # module through the worker wiring) does not load torch for it.
        import torch
        from silero_vad import get_speech_timestamps, load_silero_vad

        usable = len(pcm) - len(pcm) % _BYTES_PER_SAMPLE
        if usable == 0:
            return 0.0
        audio = torch.frombuffer(bytearray(pcm[:usable]), dtype=torch.int16).float() / 32768.0
        with self._lock:
            if self._model is None:
                self._model = load_silero_vad()
            spans = get_speech_timestamps(
                audio,
                self._model,
                threshold=SILERO_THRESHOLD,
                sampling_rate=SAMPLE_RATE,
                min_speech_duration_ms=SILERO_MIN_SPEECH_MS,
            )
        return sum(span["end"] - span["start"] for span in spans) / SAMPLE_RATE


def audio_seconds(pcm: bytes) -> float:
    return len(pcm) // _BYTES_PER_SAMPLE / SAMPLE_RATE


def speech_share(detector: SpeechDetector, pcm: bytes) -> float:
    """The part of `pcm` the detector calls speech, 0 for no audio at all."""
    seconds = audio_seconds(pcm)
    if seconds <= 0:
        return 0.0
    return min(1.0, max(0.0, detector.speech_seconds(pcm) / seconds))


def decode_pcm(video: Path, out: Path) -> bytes:
    """The first audio stream of `video` as 16 kHz mono 16-bit PCM."""
    run_ffmpeg(
        ["-i", str(video), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", str(out)],
        timeout=300,
    )
    return out.read_bytes()


def measure(video: Path, workdir: Path, detector: SpeechDetector) -> tuple[bool, float | None]:
    """(has a video stream, share of speech in its audio). The share is None for a file
    without a video stream, which is skipped unheard, and 0 for one without audio."""
    try:
        has_audio = probe_video(video).has_audio
    except PermanentProviderError as exc:
        if str(exc) == _NO_VIDEO_STREAM:
            return False, None
        raise
    if not has_audio:
        return True, 0.0
    return True, speech_share(detector, decode_pcm(video, workdir / "audio.pcm"))


class CheckVoiceActivity:
    def __init__(
        self,
        store: ObjectStore,
        detector: SpeechDetector,
        *,
        min_speech_share: float,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._detector = detector
        self._min_speech_share = min_speech_share
        self._enabled = enabled

    @activity.defn(name="check_voice")
    async def check_voice(self, spec: LocalizationInput) -> VoiceCheck:
        """`no_voice` when the share of speech in the source's audio is under the threshold."""
        tag_session(spec.aweme_id)
        if not self._enabled:
            return VoiceCheck()
        with provider_errors(), tempfile.TemporaryDirectory(prefix="check-voice-") as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            await run_blocking(download_source, self._store, spec.video_key, video)
            has_video, share = await run_blocking(measure, video, root, self._detector)
        if share is None:
            log.info("voice check: no video stream, not measured")
            return VoiceCheck(has_video=False)
        no_voice = share < self._min_speech_share
        log.info(
            "voice check: %s, %.1f%% speech (threshold %.1f%%)",
            "no voice" if no_voice else "voice",
            100 * share,
            100 * self._min_speech_share,
        )
        return VoiceCheck(speech_share=share, has_video=has_video, no_voice=no_voice)

"""The voice check of a run: the REAL `check_voice` activity over an object store on a
directory, with the detector a stand-in that answers the share of speech the test says.

The preparation fakes never write a source, so the stand-in activity first writes a real,
tiny video file of the kind the test asks for (picture and sound, picture only, sound only)
where the run's source key says, then hands over to the real one: the download, the probe and
the decode are the real code's, and `publish` copies that same file when the original goes out.
"""

import dataclasses
import functools
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from temporalio import activity

from localization.activities.check_voice import CheckVoiceActivity, audio_seconds
from localization.contracts import LocalizationInput
from localization.contracts.models import VoiceCheck
from localization.storage.object_store import ObjectStore
from tests.support.file_s3 import FileS3

SourceKind = Literal["picture_and_sound", "picture_only", "sound_only"]

_PICTURE = ["-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=2"]
_SOUND = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
_INPUTS: dict[SourceKind, list[str]] = {
    "picture_and_sound": [*_PICTURE, *_SOUND, "-shortest", "-pix_fmt", "yuv420p"],
    "picture_only": [*_PICTURE, "-pix_fmt", "yuv420p"],
    "sound_only": [*_SOUND, "-c:a", "aac"],
}


@functools.cache
def source_video(kind: SourceKind) -> bytes:
    """A two-second mp4 of that kind, made once per test process."""
    with tempfile.TemporaryDirectory(prefix="voice-source-") as tmp:
        path = Path(tmp) / "source.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-v", "error", *_INPUTS[kind], str(path)], check=True
        )
        return path.read_bytes()


@dataclasses.dataclass
class FakeDetector:
    """Calls `share` of whatever audio it is given speech, or raises `fail`; keeps the
    seconds of audio it was given each time."""

    share: float = 1.0
    fail: Exception | None = None
    measured: list[float] = dataclasses.field(default_factory=list)

    def speech_seconds(self, pcm: bytes) -> float:
        seconds = audio_seconds(pcm)
        self.measured.append(seconds)
        if self.fail is not None:
            raise self.fail
        return self.share * seconds


@dataclasses.dataclass
class VoiceFakes:
    # By default the video has a voice: every run goes on to be localized.
    detector: FakeDetector = dataclasses.field(default_factory=FakeDetector)
    source: SourceKind = "picture_and_sound"
    enabled: bool = True
    min_speech_share: float = 0.05
    calls: int = 0
    root: Path = dataclasses.field(
        default_factory=lambda: Path(tempfile.mkdtemp(prefix="voice-fakes-"))
    )

    def __post_init__(self) -> None:
        self.store = ObjectStore(FileS3(self.root), "reup-media")  # type: ignore[arg-type]

    def activities(self, store: ObjectStore | None = None) -> list[Callable]:
        """The check over `store` (the bucket `publish` copies from), or a bucket of its own."""
        fakes = self
        bucket = store or self.store
        real = CheckVoiceActivity(
            bucket,
            self.detector,
            min_speech_share=self.min_speech_share,
            enabled=self.enabled,
        )

        @activity.defn(name="check_voice")
        async def check_voice(spec: LocalizationInput) -> VoiceCheck:
            fakes.calls += 1
            bucket.put_bytes(spec.video_key, source_video(fakes.source), content_type="video/mp4")
            return await real.check_voice(spec)

        return [check_voice]

"""ZeroTTS: a Vietnamese voice that runs on this machine (onnxruntime, on the CPU).

A voice is a small file pack kept by the `zerotts` package in the user's home
(`~/.zerotts/voices`, made once with `install_voice_zip`); a run names it by id. The model
(about 1 GB of real files, not a Hugging Face cache of links, which onnxruntime refuses) is
read from `ZEROTTS_MODEL_PATH`.

One model instance speaks one request at a time, so a worker holds as many instances as
requests it speaks at once (`ZEROTTS_INSTANCES`, the `tts` queue's concurrency). They are
loaded on the first request, not at startup, so a worker that never speaks with ZeroTTS
pays nothing. `zerotts` and `numpy` are imported only when an instance is loaded or a clip
is made: they are installed only on the host speech worker (`uv pip install zerotts`), not
in the image.

The audio is a mono 16-bit WAV: the voice's samples with the DC offset taken out, the peak
kept below clipping and a short fade at both ends so a clip does not click where it is cut.
"""

import asyncio
import io
import wave
from collections.abc import Callable
from typing import Any, Protocol

from localization.activities.ffmpeg import run_blocking
from localization.config import Settings
from localization.contracts.voice import VoiceRef
from localization.providers.errors import PermanentProviderError

PEAK_LIMIT = 0.90
FADE_IN_SECONDS = 0.012
FADE_OUT_SECONDS = 0.020
# Frames kept after the model says it is done: the clip's last sound is not cut off.
END_FRAMES = 2


class Model(Protocol):
    """The part of `zerotts.ZeroTTS` this provider uses."""

    sample_rate: int

    def list_voices(self) -> list[str]: ...

    def synthesize(self, text: str, voice: str, **options: Any) -> Any: ...


def load_model(path: str, threads: int) -> Model:
    try:
        from zerotts import ZeroTTS
    except ImportError:
        raise PermanentProviderError(
            "zerotts: the package is not installed here; run "
            "`uv pip install 'zerotts>=0.1.5,<0.2'` in services/localization"
        ) from None
    try:
        model = ZeroTTS.from_pretrained(path, intra_op_num_threads=threads)
        model.warmup()
    except Exception as exc:
        raise PermanentProviderError(
            f"zerotts: the model at ZEROTTS_MODEL_PATH failed to load: {exc}"
        ) from None
    return model


def speakable(text: str) -> str:
    """The text as the model reads it best: normalized, and ending in a full stop."""
    from zerotts import normalize_vi_text

    spoken = normalize_vi_text(text.strip())
    if spoken and spoken[-1] not in ".!?":
        spoken += "."
    return spoken


def polish(samples: Any, sample_rate: int) -> Any:
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return audio
    audio = audio - float(np.mean(audio))
    peak = float(np.max(np.abs(audio)))
    if peak > PEAK_LIMIT:
        audio = audio * (PEAK_LIMIT / peak)
    fade_in = min(int(sample_rate * FADE_IN_SECONDS), audio.size // 4)
    fade_out = min(int(sample_rate * FADE_OUT_SECONDS), audio.size // 4)
    if fade_in > 1:
        audio[:fade_in] *= 0.5 * (1.0 - np.cos(np.linspace(0, np.pi, fade_in, dtype=np.float32)))
    if fade_out > 1:
        audio[-fade_out:] *= 0.5 * (1.0 + np.cos(np.linspace(0, np.pi, fade_out, dtype=np.float32)))
    return audio


def wav_bytes(samples: Any, sample_rate: int) -> bytes:
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm.tobytes())
    return buffer.getvalue()


class ZeroTts:
    """`TtsProvider` for voices of provider `zerotts`."""

    def __init__(
        self,
        model_path: str,
        *,
        instances: int = 2,
        threads: int = 2,
        loader: Callable[[str, int], Model] = load_model,
        normalize: Callable[[str], str] | None = None,
    ) -> None:
        self._path = model_path
        self._threads = threads
        self._loader = loader
        self._normalize = normalize or speakable
        self._max = instances
        self._ready: asyncio.Queue[Model] = asyncio.Queue()
        self._loaded = 0
        self._loading = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ZeroTts | None":
        """None when `ZEROTTS_MODEL_PATH` is not set: this worker cannot speak ZeroTTS
        voices, and a run that names one is refused when it asks (see `TtsByProvider`)."""
        if not settings.zerotts_model_path:
            return None
        return cls(
            settings.zerotts_model_path,
            instances=settings.zerotts_instances,
            threads=settings.zerotts_threads,
        )

    async def _borrow(self) -> Model:
        """An idle instance, a new one while fewer than the limit are loaded, else the
        next one handed back."""
        if self._ready.empty():
            async with self._loading:
                if self._ready.empty() and self._loaded < self._max:
                    model = await run_blocking(self._loader, self._path, self._threads)
                    self._loaded += 1
                    return model
        return await self._ready.get()

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        spoken = self._normalize(text)
        if not spoken:
            raise PermanentProviderError("zerotts: the text to speak is empty")
        model = await self._borrow()
        try:
            installed = model.list_voices()
            if voice.voice_id not in installed:
                raise PermanentProviderError(
                    f"zerotts: voice {voice.voice_id!r} is not installed on this machine "
                    f"(installed: {', '.join(sorted(installed))})"
                )
            raw = await run_blocking(
                lambda: model.synthesize(spoken, voice=voice.voice_id, eoa_extra_frames=END_FRAMES)
            )
        finally:
            self._ready.put_nowait(model)
        samples = polish(raw, int(model.sample_rate))
        if samples.size == 0:
            raise PermanentProviderError("zerotts: the model returned no audio")
        return wav_bytes(samples, int(model.sample_rate))

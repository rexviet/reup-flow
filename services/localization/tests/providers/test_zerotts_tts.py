"""ZeroTTS as a provider: the voice a run names, the instances a worker holds, the clip it
returns. A fake model stands in for `zerotts`, so none of this needs the package or the 1 GB
model."""

import asyncio
import io
import threading
import wave

import pytest
from tests.support.inputs import make_input

from localization.contracts.voice import VoiceRef, ZeroTtsSettings
from localization.providers.errors import PermanentProviderError
from localization.providers.tts_router import TtsByProvider
from localization.providers.zerotts_tts import ZeroTts

np = pytest.importorskip("numpy")

RATE = 24_000
VOICE = VoiceRef(provider="zerotts", voice_id="3k7WSOHjfuY9euT7VKCA", settings=ZeroTtsSettings())


class FakeModel:
    sample_rate = RATE

    def __init__(self, voices=("3k7WSOHjfuY9euT7VKCA", "maichi"), samples=None, gate=None):
        self._voices = list(voices)
        self._samples = samples
        self._gate = gate
        self.spoken: list[str] = []
        self.options: list[dict] = []

    def list_voices(self):
        return self._voices

    def synthesize(self, text, voice, **options):
        if self._gate is not None:
            self._gate.wait(timeout=5)
        self.spoken.append(text)
        self.options.append({"voice": voice, **options})
        if self._samples is not None:
            return self._samples
        t = np.arange(RATE // 2) / RATE
        return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32).reshape(1, -1)


def provider(models, **kwargs):
    made = iter(models)
    loads: list[str] = []

    def loader(path, threads):
        loads.append(f"{path}:{threads}")
        return next(made)

    zt = ZeroTts("/models/zerotts", loader=loader, normalize=lambda t: t.strip(), **kwargs)
    return zt, loads


def read(wav_bytes: bytes):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


async def test_a_line_comes_back_as_a_mono_16_bit_wav_of_the_models_rate() -> None:
    model = FakeModel()
    zt, _ = provider([model])
    audio = await zt.synthesize("Xin chào.", VOICE)
    assert read(audio) == (1, 2, RATE, RATE // 2)
    assert model.options == [{"voice": "3k7WSOHjfuY9euT7VKCA", "eoa_extra_frames": 2}]


async def test_the_clip_never_clips_and_has_no_dc_offset_and_fades_at_its_ends() -> None:
    loud = np.full((1, RATE), 1.6, dtype=np.float32) + 0.2
    zt, _ = provider([FakeModel(samples=loud)])
    audio = await zt.synthesize("To.", VOICE)
    with wave.open(io.BytesIO(audio)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert int(np.abs(pcm).max()) <= int(0.90 * 32767) + 1
    assert pcm[0] == 0
    assert abs(int(pcm[-1])) <= 1


async def test_a_voice_that_is_not_installed_is_refused_and_the_installed_ones_are_named() -> None:
    zt, _ = provider([FakeModel(voices=("maichi",))])
    with pytest.raises(PermanentProviderError, match="not installed.*maichi"):
        await zt.synthesize("Xin chào.", VOICE)


async def test_empty_text_and_empty_audio_are_refused_not_stored_as_silence() -> None:
    zt, _ = provider([FakeModel()])
    with pytest.raises(PermanentProviderError, match="empty"):
        await zt.synthesize("   ", VOICE)
    zt, _ = provider([FakeModel(samples=np.zeros((1, 0), dtype=np.float32))])
    with pytest.raises(PermanentProviderError, match="no audio"):
        await zt.synthesize("Xin chào.", VOICE)


async def test_the_model_is_loaded_on_the_first_line_not_before_and_reused() -> None:
    zt, loads = provider([FakeModel(), FakeModel()], instances=2, threads=3)
    assert loads == []
    await zt.synthesize("Một.", VOICE)
    await zt.synthesize("Hai.", VOICE)
    assert loads == ["/models/zerotts:3"]


async def test_lines_spoken_at_once_use_one_instance_each_and_never_more_than_the_limit() -> None:
    gate = threading.Event()
    models = [FakeModel(gate=gate) for _ in range(2)]
    zt, loads = provider(models, instances=2)
    tasks = [asyncio.create_task(zt.synthesize(f"Câu {i}.", VOICE)) for i in range(3)]
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(loads) == 2:
            break
    assert len(loads) == 2  # the third line waits for an instance, it does not load a third
    gate.set()
    await asyncio.gather(*tasks)
    assert len(loads) == 2
    assert sum(len(m.spoken) for m in models) == 3


def test_settings_without_a_model_path_give_no_provider_and_with_one_give_it() -> None:
    from localization.config import Settings

    base = make_settings()
    assert ZeroTts.from_settings(base) is None
    configured = base.model_copy(
        update={
            "zerotts_model_path": "/models/zerotts",
            "zerotts_instances": 3,
            "zerotts_threads": 4,
        }
    )
    zt = ZeroTts.from_settings(configured)
    assert isinstance(zt, ZeroTts)
    assert isinstance(base, Settings)


def make_settings():
    from localization.config import Settings

    return Settings(_env_file=None)


async def test_a_zerotts_voice_goes_to_zerotts_and_a_worker_without_it_refuses() -> None:
    class Spy:
        def __init__(self):
            self.heard = []

        async def synthesize(self, text, voice):
            self.heard.append((text, voice.provider))
            return b"RIFF"

    zero, studio = Spy(), Spy()
    router = TtsByProvider(studio, None, zero)
    await router.synthesize("Xin chào.", VOICE)
    await router.synthesize("Chào.", make_input().voice)
    assert zero.heard == [("Xin chào.", "zerotts")]
    assert studio.heard == [("Chào.", "voicestudio")]
    with pytest.raises(PermanentProviderError, match="zerotts"):
        await TtsByProvider(studio).synthesize("Xin chào.", VOICE)


def test_a_zerotts_voice_cannot_carry_a_speed_or_a_credential() -> None:
    with pytest.raises(ValueError):
        ZeroTtsSettings(speed=1.2)  # type: ignore[call-arg]
    assert (
        VoiceRef.model_validate({"provider": "zerotts", "voice_id": "x", "settings": {}}).settings
        == ZeroTtsSettings()
    )


def test_a_zerotts_voice_has_a_fixed_speed_of_one_that_cannot_be_set() -> None:
    assert ZeroTtsSettings().speed == 1.0
    with pytest.raises(ValueError):
        ZeroTtsSettings.model_validate({"speed": 1.5})


async def test_the_speaking_rate_of_a_zerotts_voice_is_measured_like_any_other_voice() -> None:
    """The measured rate is divided by the voice's speed, which a ZeroTTS voice has none of."""
    from temporalio.testing import ActivityEnvironment

    from localization.activities.voice_rate import SAMPLE_TEXT, VoiceRateActivity
    from localization.text.vi_syllables import count_syllables

    class Store:
        def __init__(self) -> None:
            self.put_rate: float | None = None

        async def get(self, key):
            return None

        async def put(self, key, rate):
            self.put_rate = rate
            return rate

    class Speaks:
        async def synthesize(self, text, voice):
            seconds = count_syllables(SAMPLE_TEXT) / 5.0  # five syllables a second
            frames = round(RATE * seconds)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(b"\x00\x10" * frames)
            return buffer.getvalue()

    store = Store()
    activity = VoiceRateActivity(Speaks(), store, model=None)  # type: ignore[arg-type]
    rate = await ActivityEnvironment().run(activity.voice_rate, VOICE)
    assert rate == pytest.approx(5.0, rel=0.1)
    assert store.put_rate == rate

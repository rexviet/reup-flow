"""Calibrates the syllable estimate against the real voice. Run by hand with the
repo's .env (VOICESTUDIO_URL, TTS_MODEL, VOICE_ID) pointing at a reachable
VoiceStudio that has a Vietnamese voice:

    uv run pytest tests/text/test_vi_syllables_live.py -m live -s

Each sentence is spoken once; the rate is the median over the set, and every
sentence's estimate at that rate must land within `TOLERANCE` of the real length.
"""

import statistics
import tempfile
from pathlib import Path

import pytest

from localization.activities.ffmpeg import probe_duration_ms
from localization.config import Settings, voice_ref_from_settings
from localization.providers.openai_tts import OpenAICompatTts
from localization.text.vi_syllables import count_syllables, estimate_ms

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[4]
TOLERANCE = 0.25

SENTENCES = [
    "năm 2024 mua iPhone",
    "Hôm nay trời đẹp quá, chúng ta cùng đi dạo nhé",
    "Giá giảm đến 30% chỉ còn 1.500.000 đồng",
    "Mình đã dùng chiếc điện thoại này suốt 15 ngày",
    "Đăng ký kênh TikTok của mình để xem thêm nhiều video hay",
    "Pin rất bền, sạc một lần dùng cả ngày",
    "Cảm ơn các bạn đã xem video, hẹn gặp lại",
    "Chiếc máy này có giá 3,5 triệu đồng",
]


async def test_the_estimate_matches_the_real_voice_within_tolerance() -> None:
    settings = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    if not (settings.voicestudio_url and settings.tts_model and settings.voice_id):
        pytest.skip("VOICESTUDIO_URL, TTS_MODEL and VOICE_ID are not set")
    tts = OpenAICompatTts.from_settings(settings)
    voice = voice_ref_from_settings(settings)
    measured: list[tuple[str, int]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for text in SENTENCES:
            wav = Path(tmp) / "s.wav"
            wav.write_bytes(await tts.synthesize(text, voice))
            measured.append((text, probe_duration_ms(wav)))
    rate = statistics.median(count_syllables(t) / (ms / 1000) for t, ms in measured)
    print(f"median rate: {rate:.2f} syllables/s")
    for text, real in measured:
        estimate = estimate_ms(text, rate)
        print(f"{real} ms real, {estimate} ms estimated: {text}")
        assert abs(estimate - real) <= TOLERANCE * real, text

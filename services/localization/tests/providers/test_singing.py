"""The singing detector: two audio requests per clip with different questions, and a clip is
sung only when both answers say so."""

import base64
import json

import httpx
import pytest

from localization.config import Settings
from localization.providers.errors import PermanentProviderError, TransientProviderError
from localization.providers.singing import SingingDetector

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32


def detector(
    label: str | None,
    melodic: str | None,
    status: int = 200,
    seen: list | None = None,
) -> SingingDetector:
    """A detector whose model answers `label` to the label question and `melodic` to the
    melody question."""

    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        prompt = json.loads(request.content)["messages"][0]["content"][0]["text"]
        reply = melodic if "sustained pitched notes" in prompt else label
        return httpx.Response(status, json={"choices": [{"message": {"content": reply}}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    return SingingDetector("https://router.test/api/v1", "k-test", "audio/model", http=http)


SING, SPEAK = '{"label": "singing"}', '{"label": "speech"}'
MELODIC, FLAT = '{"melodic": true}', '{"melodic": false}'


async def test_the_clip_is_sent_twice_as_wav_audio_with_reasoning_off() -> None:
    seen: list[httpx.Request] = []
    assert await detector(SING, MELODIC, seen=seen).is_singing(WAV) is True
    assert len(seen) == 2
    prompts = set()
    for request in seen:
        assert str(request.url) == "https://router.test/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer k-test"
        body = json.loads(request.content)
        assert body["model"] == "audio/model"
        assert body["reasoning"] == {"enabled": False}
        prompts.add(body["messages"][0]["content"][0]["text"])
        audio = body["messages"][0]["content"][1]
        assert audio["type"] == "input_audio"
        assert audio["input_audio"]["format"] == "wav"
        assert base64.b64decode(audio["input_audio"]["data"]) == WAV
    assert len(prompts) == 2


@pytest.mark.parametrize(
    "label,melodic,expected",
    [
        (SING, MELODIC, True),
        ("```json\n" + SING + "\n```", MELODIC, True),
        (SING, FLAT, False),
        (SPEAK, MELODIC, False),
        (SPEAK, FLAT, False),
    ],
)
async def test_a_clip_is_sung_only_when_both_answers_say_so(
    label: str, melodic: str, expected: bool
) -> None:
    assert await detector(label, melodic).is_singing(WAV) is expected


@pytest.mark.parametrize(
    "label,melodic",
    [
        (None, MELODIC),
        (SING, None),
        ("It sounds like singing.", MELODIC),
        ('{"label": "maybe"}', MELODIC),
        (SING, '{"melodic": "yes"}'),
        (SING, '{"sung": true}'),
    ],
)
async def test_an_unusable_reply_is_worth_asking_again(
    label: str | None, melodic: str | None
) -> None:
    with pytest.raises(TransientProviderError):
        await detector(label, melodic).is_singing(WAV)


async def test_a_refused_request_is_permanent() -> None:
    with pytest.raises(PermanentProviderError):
        await detector(SING, MELODIC, status=400).is_singing(WAV)


def test_no_model_configured_means_no_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    assert SingingDetector.from_settings(Settings()) is None
    monkeypatch.setenv("SINGING_MODEL", "google/gemini-3.1-flash-lite")
    built = SingingDetector.from_settings(Settings())
    assert built is not None and built.model_id == "google/gemini-3.1-flash-lite"

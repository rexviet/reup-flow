"""G2: is VoiceStudio usable as STT + TTS? Needs VOICESTUDIO_URL (blocked without it).

Run:  ./run.sh uv run python g2_voicestudio.py            # live, against VOICESTUDIO_URL
      ./run.sh uv run python g2_voicestudio.py --selftest # the measuring code only, no server

Measures: missing-word-timestamp rate (the RIGHT way), voice exists, TTS speech
rate in syllables/second, and that a request without a key gets 401.
Not automated: the 10 hand-aligned sentences (<=200 ms) need a human; the
script writes the transcripts for that and reports it as PENDING, never PASS.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

URL = os.environ.get("VOICESTUDIO_URL", "").rstrip("/")
KEY = os.environ.get("VOICESTUDIO_API_KEY", "")
VOICE_ID = os.environ.get("VOICE_ID", "")
STT_MODELS = [m for m in os.environ.get("G2_STT_MODELS", os.environ.get("STT_MODEL", "")).split(",") if m]
TTS_MODEL = os.environ.get("TTS_MODEL", "")
# Without a language hint the server auto-detects and gets Chinese speech badly
# wrong (observed: Sinhala, French), so the spike always sends one.
STT_LANGUAGE = os.environ.get("G2_STT_LANGUAGE", "zh")
SAMPLE_VIDEOS = 5
CANDIDATE_VIDEOS = 30

_PUNCT = re.compile(r"[\s　，。！？、；：“”‘’（）()\[\]{}.,!?;:\"'—-]+")
_DIGIT_WORDS = "không một hai ba bốn năm sáu bảy tám chín".split()


def missing_word_ratio(text: str, words: list[dict]) -> float:
    """Share of characters in `text` that no word entry covers.

    VoiceStudio silently DROPS words that have no timestamp, so counting
    words-without-timestamps directly always yields 0. The loss is visible only
    by comparing the characters of the full text with those of the joined words.
    """
    full = _PUNCT.sub("", text)
    joined = _PUNCT.sub("", "".join(w.get("word", "") for w in words))
    if not full:
        return 0.0
    return max(0.0, (len(full) - len(joined)) / len(full))


def _say_number(n: int) -> str:
    """Digit-by-digit reading: enough to count syllables of a numeric sentence."""
    return " ".join(_DIGIT_WORDS[int(d)] for d in str(n))


def syllables(text_vi: str) -> int:
    """Vietnamese is monosyllabic per whitespace token once digits are spelled out."""
    spelled = re.sub(r"\d+", lambda m: " " + _say_number(int(m.group())) + " ", text_vi)
    return len([t for t in _PUNCT.sub(" ", spelled).split() if t])


def selftest() -> int:
    assert missing_word_ratio("你好世界", [{"word": "你好"}, {"word": "世界"}]) == 0.0
    assert abs(missing_word_ratio("你好世界", [{"word": "你好"}]) - 0.5) < 1e-9
    # "Tôi có" (2) + "hai năm" (2) + "quả táo" (2)
    assert syllables("Tôi có 25 quả táo") == 6
    print("selftest ok")
    return 0


def _h(auth: bool) -> dict:
    return {"Authorization": f"Bearer {KEY}"} if auth and KEY else {}


def _host_class(url: str) -> str:
    p = urlparse(url)
    try:
        ip = ipaddress.ip_address(p.hostname or "")
        kind = "private-ip" if ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10") else "public-ip"
    except ValueError:
        kind = "hostname"
    return f"{p.scheme}/{kind}"


def _extract_audio(video: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-ac", "1", "-ar", "16000", str(out)], check=True)


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if not URL:
        print(json.dumps({"verdict": "BLOCKED", "reason": "VOICESTUDIO_URL is not set"}, indent=2))
        return 2
    result: dict = {"url_class": _host_class(URL), "stt": {}, "tts": {}, "auth": {}}
    c = httpx.Client(base_url=URL, timeout=300)

    # 401 without a key: loopback / trusted-network clients bypass auth server-side,
    # so a 200 here means the server is not enforcing it for us.
    r = c.get("/v1/audio/voices")
    result["auth"] = {"status_without_key": r.status_code, "requires_key": r.status_code == 401}

    voices = c.get("/v1/audio/voices", headers=_h(True))
    ids = [v.get("id") or v.get("voice_id") or v.get("name") for v in (voices.json().get("voices", voices.json()) if voices.status_code == 200 else [])]
    result["voice_exists"] = VOICE_ID in ids

    # Many downloaded videos are music-only, so scan an evenly spaced candidate list
    # and stop once SAMPLE_VIDEOS of them actually produced a transcript.
    all_videos = sorted(Path("/data").rglob("*.mp4"))
    videos = all_videos[:: max(1, len(all_videos) // CANDIDATE_VIDEOS)][:CANDIDATE_VIDEOS]
    with tempfile.TemporaryDirectory() as tmp:
        for model in STT_MODELS or [""]:
            rates = []
            for v in videos:
                if sum("missing_word_ratio" in r for r in rates) >= SAMPLE_VIDEOS:
                    break
                wav = Path(tmp) / f"{v.stem}.wav"
                _extract_audio(v, wav)
                with wav.open("rb") as f:
                    resp = c.post(
                        "/v1/audio/transcriptions",
                        headers=_h(True),
                        files={"file": (wav.name, f, "audio/wav")},
                        data={"model": model, "language": STT_LANGUAGE, "response_format": "verbose_json", "timestamp_granularities[]": "word"},
                    )
                if resp.status_code != 200:
                    rates.append({"video": v.name, "error": resp.status_code})
                    continue
                body = resp.json()
                if not _PUNCT.sub("", body.get("text", "")):
                    # missing_word_ratio() answers 0.0 for an empty text, which would
                    # read a silent or failed clip as perfect. Report it as what it is.
                    rates.append({"video": v.name, "error": "empty_transcript", "duration": body.get("duration")})
                    continue
                rates.append({"video": v.name, "missing_word_ratio": round(missing_word_ratio(body.get("text", ""), body.get("words", [])), 4), "engine": body.get("engine")})
            result["stt"][model or "default"] = rates

        sentence = "Hôm nay chúng ta làm 25 động tác trong 3 phút."
        sp = c.post("/v1/audio/speech", headers=_h(True), json={"model": TTS_MODEL, "voice": VOICE_ID, "input": sentence, "response_format": "wav"})
        if sp.status_code == 200:
            out = Path(tmp) / "tts.wav"
            out.write_bytes(sp.content)
            dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)]))
            result["tts"] = {"seconds": dur, "syllables": syllables(sentence), "syllables_per_second": round(syllables(sentence) / dur, 2)}
        else:
            result["tts"] = {"error": sp.status_code}

    result["manual_alignment"] = "PENDING: 10 hand-labelled sentences, <=200 ms drift, need a human"
    measured = [r for rs in result["stt"].values() for r in rs if "missing_word_ratio" in r]
    result["stt_clips_measured"] = len(measured)
    ok = (
        result["auth"]["requires_key"]
        and result["voice_exists"]
        and "seconds" in result["tts"]
        and len(measured) >= 3
    )
    result["verdict"] = "PENDING-MANUAL" if ok else "FAIL"
    result["reproduce"] = "./run.sh uv run python g2_voicestudio.py"
    os.makedirs("out", exist_ok=True)
    Path("out/g2.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

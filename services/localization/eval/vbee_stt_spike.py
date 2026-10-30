"""Live spike: can Vbee's STT API transcribe Mandarin, and with what timestamps?

Run: uv run python eval/vbee_stt_spike.py AUDIO.wav --mode sync|async --out DIR
     [--field language=zh]

Sends one WAV to POST https://api.vbee.vn/v1/stt (multipart: audioContent, mode). With
`async`, it polls GET /v1/stt/transcripts/{id} until COMPLETED or FAILED, or the timeout.
The raw JSON of every response is written to DIR, and a short summary is printed.
VBEE_API_TOKEN (Bearer JWT) and VBEE_APP_ID come from the repo .env. Neither is ever printed:
errors show the HTTP status and a body with both values scrubbed out.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from common import load_env

BASE_URL = "https://api.vbee.vn/v1/stt"
POLL_EVERY_S = 3.0
TERMINAL = {"COMPLETED", "FAILED"}


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _decode(response: httpx.Response, secrets: list[str]) -> Any:
    """The JSON body, or a dict holding the scrubbed raw text when the body is not JSON."""
    try:
        return response.json()
    except ValueError:
        return {"_non_json_body": _scrub(response.text[:2000], secrets)}


def _save(out: Path, name: str, payload: Any, secrets: list[str]) -> Path:
    path = out / name
    path.write_text(_scrub(json.dumps(payload, ensure_ascii=False, indent=2), secrets) + "\n")
    return path


def _summary(body: Any, latency_s: float) -> None:
    if not isinstance(body, dict):
        print(f"unexpected body type: {type(body).__name__}")
        return
    utterances = body.get("utterances") or []
    word_keys = {key for u in utterances if isinstance(u, dict) for key in u}
    print(f"status:        {body.get('status')}")
    print(f"language:      {body.get('language', '(not in response)')}")
    print(f"audio seconds: {body.get('audioDurationSeconds')}")
    print(f"segments:      {len(utterances)}  (fields: {sorted(word_keys)})")
    has_words = any(k in word_keys for k in ("words", "tokens", "chars"))
    print(f"granularity:   {'word/char' if has_words else 'utterance only'}")
    print(f"latency:       {latency_s:.2f} s (submit to final result)")
    print(f"transcript:    {body.get('transcript')!r}")
    for u in utterances:
        if isinstance(u, dict):
            print(f"  [{u.get('startTime')} - {u.get('endTime')}] {u.get('text')!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", type=Path, help="WAV file, 8/16/22.05/32/44.1/48 kHz")
    parser.add_argument("--mode", choices=("sync", "async"), required=True)
    parser.add_argument("--out", type=Path, required=True, help="directory for raw JSON")
    parser.add_argument("--field", action="append", default=[], help="extra form field k=v")
    parser.add_argument("--timeout", type=float, default=300.0, help="async poll timeout, s")
    args = parser.parse_args()

    if not args.audio.is_file():
        print(f"no such audio file: {args.audio}", file=sys.stderr)
        return 2
    extra: dict[str, str] = {}
    for field in args.field:
        key, sep, value = field.partition("=")
        if not sep or not key:
            print(f"--field must be key=value, got {field!r}", file=sys.stderr)
            return 2
        extra[key] = value

    load_env()
    token = os.environ.get("VBEE_API_TOKEN", "")
    app_id = os.environ.get("VBEE_APP_ID", "")
    missing = [n for n, v in (("VBEE_API_TOKEN", token), ("VBEE_APP_ID", app_id)) if not v]
    if missing:
        print(f"missing in .env: {', '.join(missing)}", file=sys.stderr)
        return 2
    secrets = [token, app_id]
    headers = {"Authorization": f"Bearer {token}", "App-Id": app_id}

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.audio.stem}-{args.mode}" + "".join(f"-{k}_{v}" for k, v in extra.items())
    data = {"mode": args.mode, **extra}

    with httpx.Client(timeout=120.0, headers=headers) as client:
        started = time.monotonic()
        try:
            with args.audio.open("rb") as audio:
                response = client.post(
                    BASE_URL,
                    data=data,
                    files={"audioContent": (args.audio.name, audio, "audio/wav")},
                )
        except httpx.HTTPError as exc:
            print(f"submit failed: {type(exc).__name__}", file=sys.stderr)
            return 1
        body = _decode(response, secrets)
        print(f"submit: HTTP {response.status_code} in {time.monotonic() - started:.2f} s")
        print(f"saved {_save(args.out, f'{stem}-submit.json', body, secrets)}")
        if response.status_code >= 300:
            print(_scrub(json.dumps(body, ensure_ascii=False), secrets), file=sys.stderr)
            return 1

        polls = 0
        while isinstance(body, dict) and body.get("status") not in TERMINAL:
            transcript_id = body.get("transcriptId")
            if not transcript_id:
                print("no transcriptId to poll", file=sys.stderr)
                return 1
            if time.monotonic() - started > args.timeout:
                print(f"timed out after {args.timeout:.0f} s, status {body.get('status')}")
                return 1
            time.sleep(POLL_EVERY_S)
            polls += 1
            try:
                response = client.get(f"{BASE_URL}/transcripts/{transcript_id}")
            except httpx.HTTPError as exc:
                print(f"poll failed: {type(exc).__name__}", file=sys.stderr)
                return 1
            body = _decode(response, secrets)
            if response.status_code >= 300:
                _save(args.out, f"{stem}-poll-error.json", body, secrets)
                print(f"poll: HTTP {response.status_code}", file=sys.stderr)
                print(_scrub(json.dumps(body, ensure_ascii=False), secrets), file=sys.stderr)
                return 1
        latency = time.monotonic() - started

    print(f"polls: {polls}")
    print(f"saved {_save(args.out, f'{stem}-final.json', body, secrets)}")
    _summary(body, latency)
    return 0


if __name__ == "__main__":
    sys.exit(main())

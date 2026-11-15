#!/usr/bin/env python3
"""Compare local VoiceStudio STT with only multilingual toggled; no TTS or LLM calls."""
import argparse
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def multipart(audio, model, multilingual):
    boundary = "voicestudio-spike-" + uuid.uuid4().hex
    fields = [
        ("model", model), ("response_format", "verbose_json"), ("temperature", "0"),
        ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment"),
        ("multilingual", str(multilingual).lower()),
    ]
    parts = []
    for key, value in fields:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                      f'\r\n\r\n{value}\r\n').encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  'filename="vocals.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode())
    parts += [audio, f"\r\n--{boundary}--\r\n".encode()]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def summary(body):
    segments = body.get("segments", [])
    text = " ".join(s.get("text", "") for s in segments)
    longest = run = 0
    previous = None
    for token in re.findall(r"\w+", text.lower()):
        run = run + 1 if token == previous else 1
        longest = max(longest, run)
        previous = token
    windows = []
    for start, end in [(20, 34), (96, 106)]:
        windows.append({"start_s": start, "end_s": end, "text": " ".join(
            s.get("text", "").strip() for s in segments
            if float(s.get("start", 0)) < end and float(s.get("end", 0)) > start
        )})
    return {
        "top_level_language": body.get("language"), "segments": len(segments),
        "hangul_chars": sum("\uac00" <= c <= "\ud7a3" for c in text),
        "han_chars": sum("\u4e00" <= c <= "\u9fff" for c in text),
        "segments_with_language_metadata": sum(bool(s.get("language")) for s in segments),
        "longest_repeated_token_run": longest, "windows": windows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:3900")
    parser.add_argument("--model", default="faster-whisper")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    headers = {}
    if os.environ.get("VOICESTUDIO_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["VOICESTUDIO_API_KEY"]
    request = urllib.request.Request(base + "/openapi.json", headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        schema = json.load(response)
    operation = schema["paths"]["/v1/audio/transcriptions"]["post"]
    ref = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"]
    properties = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]
    if "multilingual" not in properties:
        raise SystemExit("The live API does not advertise multilingual; load the runtime patch first.")
    if any((args.output_dir / name).exists() for name in [
        "multilingual-off.json", "multilingual-on.json", "comparison.json",
    ]):
        raise SystemExit("Output already contains spike results; choose a fresh output directory.")
    audio = args.input.read_bytes()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {"audio_sha256": hashlib.sha256(audio).hexdigest(), "model": args.model,
              "temperature": 0, "runs": {}}
    for enabled in [False, True]:
        label = "multilingual-on" if enabled else "multilingual-off"
        body, content_type = multipart(audio, args.model, enabled)
        request = urllib.request.Request(base + "/v1/audio/transcriptions", data=body,
                                        headers={**headers, "Content-Type": content_type})
        print("Transcribing", label, flush=True)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise SystemExit(f"{label}: provider HTTP {error.code}; response omitted.") from None
        (args.output_dir / (label + ".json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        report["runs"][label] = {"elapsed_s": round(time.monotonic() - started, 2),
                                 **summary(result)}
        (args.output_dir / "comparison.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report["runs"][label], ensure_ascii=False), flush=True)
    print("Saved comparison to", args.output_dir / "comparison.json", flush=True)


if __name__ == "__main__":
    main()

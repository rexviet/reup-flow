"""The run-level measures: a dubbed video without speech, speech left undubbed, a line too
long for the TTS cache, and whether the source's own voice is still in the output."""

import base64
import json
import math
import subprocess
import wave
from pathlib import Path

import pipeline_eval
import pytest
from common import History

from localization.activities.synthesize import MAX_METADATA_TEXT_BYTES


def _payload(value: object) -> dict:
    return {"payloads": [{"data": base64.b64encode(json.dumps(value).encode()).decode()}]}


def _history(*calls: tuple[str, list, object], result: object = None) -> History:
    events: list[dict] = []
    for name, args, answer in calls:
        scheduled = len(events) + 1
        events.append(
            {
                "eventId": str(scheduled),
                "activityTaskScheduledEventAttributes": {
                    "activityType": {"name": name},
                    "input": {"payloads": [_payload(a)["payloads"][0] for a in args]},
                },
            }
        )
        events.append(
            {
                "eventId": str(scheduled + 1),
                "activityTaskCompletedEventAttributes": {
                    "scheduledEventId": str(scheduled),
                    "result": _payload(answer),
                },
            }
        )
    if result is not None:
        events.append(
            {
                "eventId": str(len(events) + 1),
                "workflowExecutionCompletedEventAttributes": {"result": _payload(result)},
            }
        )
    return History(events)


def test_a_run_is_read_from_its_history() -> None:
    long_line = "rất dài " * 200
    history = _history(
        ("ingest", [{}], {"vocals_key": "work/1/r0/vocals.wav", "bgm_key": "work/1/r0/bgm.wav"}),
        ("transcribe", [{}, "k"], {"transcript_key": "k", "segment_count": 2}),
        ("assemble_script", [{}, "t", "s", {"lines": [{"id": 0, "text_vi": "chào"}]}], {}),
        (
            "write_dubbed_script",
            [{}, {}, {"items": [{"id": 0, "text_vi": long_line, "stretch_ratio": 1.0}]}],
            {},
        ),
        result={"publish": {"outcome": "needs_human", "review_key": "review/1-g1.mp4"}},
    )
    run = pipeline_eval.fill_from_history(pipeline_eval.VideoRun("1", "speech"), history)
    assert run.segments == 2 and run.dubbed is True
    assert (run.vocals_key, run.bgm_key) == ("work/1/r0/vocals.wav", "work/1/r0/bgm.wav")
    assert run.max_line_bytes == pipeline_eval.line_bytes(long_line) > MAX_METADATA_TEXT_BYTES
    assert run.output_key == "review/1-g1.mp4"


def test_line_bytes_is_the_base64_the_cache_records() -> None:
    assert pipeline_eval.line_bytes("chào") == len(base64.b64encode("chào".encode()))


def test_the_summary_tells_false_dubs_from_missed_speech() -> None:
    runs = [
        pipeline_eval.VideoRun("sing", "singing", status="needs_human", segments=2),
        pipeline_eval.VideoRun("quiet", "silence", status="published", segments=0),
        pipeline_eval.VideoRun("talk", "speech", status="published", segments=4),
        pipeline_eval.VideoRun("talk2", "speech_over_music", status="published", segments=0),
        pipeline_eval.VideoRun(
            "long",
            "speech",
            status="failed",
            segments=9,
            reasons=["TextTooLong: the line is too long"],
            max_line_bytes=1520,
        ),
        pipeline_eval.VideoRun("never", "speech"),  # no run yet: counted nowhere
    ]
    summary = pipeline_eval.summarize(runs)
    assert summary["false_dub_rate"] == 0.5 and summary["false_dubs"] == ["sing"]
    assert summary["missed_speech_rate"] == pytest.approx(1 / 3)
    assert summary["missed_speech"] == ["talk2"]
    assert summary["text_too_long_rate"] == pytest.approx(1 / 5)
    assert (summary["max_line_bytes"], summary["line_bytes_limit"]) == (1520, 1400)


def test_an_unknown_label_is_refused(tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text('{"aweme_id": "1", "audio": "probably music"}\n')
    with pytest.raises(ValueError):
        pipeline_eval.read_labels(labels)


def _tone(path: Path, frequency: float, seconds: float = 1.0, amplitude: int = 6000) -> None:
    rate = pipeline_eval.SAMPLE_RATE
    frames = bytearray()
    for i in range(int(rate * seconds)):
        value = int(amplitude * math.sin(2 * math.pi * frequency * i / rate))
        frames += value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))


def _mix(out: Path, *inputs: Path) -> None:
    filters = f"amix=inputs={len(inputs)}:normalize=0" if len(inputs) > 1 else "anull"
    argv = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    for item in inputs:
        argv += ["-i", str(item)]
    argv += ["-filter_complex", filters, "-c:a", "aac", "-b:a", "192k", str(out)]
    subprocess.run(argv, check=True)


@pytest.mark.parametrize(("kept", "expected"), [(True, 1.0), (False, 0.0)])
def test_the_source_voice_is_found_in_an_output_that_kept_it(
    tmp_path: Path, kept: bool, expected: float
) -> None:
    vocals, bgm, output = tmp_path / "vocals.wav", tmp_path / "bgm.wav", tmp_path / "out.m4a"
    _tone(vocals, 440)
    _tone(bgm, 97)
    _mix(output, *((bgm, vocals) if kept else (bgm,)))
    assert pipeline_eval.original_vocals_kept(output, vocals, bgm) == pytest.approx(
        expected, abs=0.1
    )

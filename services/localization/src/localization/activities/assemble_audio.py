"""Step 6: the dubbed track.

Every clip is sped (or slowed) by its stretch ratio and placed at its segment's start;
the clips are mixed into one voice track; the background music is ducked under it;
the result is loudness-normalised in two passes to the configured target. The output
must be as long as the video, because a track that is not would drift against the
picture.

This module takes a script and clips and produces audio. It never writes or changes
text, so it must not reach an agent.
"""

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.activities.ffmpeg import probe_duration_ms, run_blocking, run_ffmpeg
from localization.activities.script_document import read_script
from localization.contracts import (
    DubbedAudio,
    DubSegmentsDocument,
    IngestResult,
    LocalizationInput,
    LocalizedScriptRef,
)
from localization.observability.tracing import tag_session
from localization.providers.errors import PermanentProviderError
from localization.storage.object_store import ObjectStore, work_key

SAMPLE_RATE = 44100
# The track and the video may differ by this much and no more.
LENGTH_TOLERANCE_MS = 50
LOUDNORM_LRA = 11

# Music drops under the voice: heavy compression keyed on the voice track.
DUCK = "threshold=0.03:ratio=10:attack=20:release=300"

_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def build_filter_graph(placements: list[tuple[int, float]], duration_ms: int) -> str:
    """The ffmpeg filter graph. Input 0 is the music; inputs 1.. are the clips, in
    the order of `placements` (start in ms, stretch ratio). Pure, so the arithmetic
    of where each clip goes can be tested without ffmpeg."""
    seconds = duration_ms / 1000
    fmt = f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo"
    lines = [f"[0:a]{fmt},apad=whole_dur={seconds:.3f},atrim=end={seconds:.3f}[bgm]"]
    labels = []
    for index, (start_ms, ratio) in enumerate(placements, start=1):
        label = f"v{index}"
        labels.append(f"[{label}]")
        lines.append(f"[{index}:a]{fmt},atempo={ratio:.6f},adelay={start_ms}:all=1[{label}]")
    if labels:
        # The voice track is padded to the video's length: the ducker ends when its
        # control input does, and a track that ended with the last line would cut the
        # music off there.
        lines.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
            f"apad=whole_dur={seconds:.3f}[voice]"
        )
        lines.append("[voice]asplit=2[vkey][vmix]")
        lines.append(f"[bgm][vkey]sidechaincompress={DUCK}[ducked]")
        lines.append("[ducked][vmix]amix=inputs=2:normalize=0:duration=longest[mix]")
    else:
        lines.append("[bgm]anull[mix]")
    return ";\n".join(lines)


def _loudnorm(target: float, peak: float) -> str:
    return f"loudnorm=I={target}:TP={peak}:LRA={LOUDNORM_LRA}"


def parse_loudnorm(stderr: bytes) -> dict[str, str]:
    """The measurement block loudnorm prints at the end of a first pass."""
    blocks = _JSON_RE.findall(stderr.decode(errors="replace"))
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and "input_i" in data:
            return {k: str(v) for k, v in data.items()}
    raise PermanentProviderError("ffmpeg: loudnorm did not report a measurement")


def measure_loudness(path: Path, target: float, peak: float) -> dict[str, str]:
    result = run_ffmpeg(
        ["-i", str(path), "-af", f"{_loudnorm(target, peak)}:print_format=json", "-f", "null", "-"],
        loglevel="info",
    )
    return parse_loudnorm(result.stderr)


def render(
    *,
    bgm: Path,
    clips: list[Path],
    placements: list[tuple[int, float]],
    duration_ms: int,
    out: Path,
    work: Path,
    target: float,
    peak: float,
) -> None:
    graph = work / "graph.txt"
    graph.write_text(build_filter_graph(placements, duration_ms))
    mixed = work / "mix.wav"
    inputs: list[str] = []
    for path in [bgm, *clips]:
        inputs += ["-i", str(path)]
    run_ffmpeg(
        [
            *inputs,
            "-filter_complex_script",
            str(graph),
            "-map",
            "[mix]",
            "-c:a",
            "pcm_f32le",
            str(mixed),
        ]
    )
    measured = measure_loudness(mixed, target, peak)
    if measured["input_i"] in ("-inf", "inf") or measured["input_thresh"] in ("-inf", "inf"):
        raise ApplicationError("the mixed track is silent", type="SilentAudio", non_retryable=True)
    normalise = (
        f"{_loudnorm(target, peak)}:measured_I={measured['input_i']}"
        f":measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}"
        f":measured_thresh={measured['input_thresh']}:offset={measured['target_offset']}"
        ":linear=true"
    )
    run_ffmpeg(
        ["-i", str(mixed), "-af", normalise, "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(out)]
    )


def check_length(path: Path, duration_ms: int) -> None:
    actual = probe_duration_ms(path)
    if abs(actual - duration_ms) > LENGTH_TOLERANCE_MS:
        raise ApplicationError(
            f"the dubbed track is {actual} ms and the video is {duration_ms} ms",
            type="AudioLengthMismatch",
            non_retryable=True,
        )


class AssembleAudioActivity:
    def __init__(self, store: ObjectStore, *, loudness_lufs: float, true_peak_dbtp: float) -> None:
        self._store = store
        self._target = loudness_lufs
        self._peak = true_peak_dbtp

    @activity.defn(name="assemble_audio")
    async def assemble_audio(
        self,
        spec: LocalizationInput,
        script: LocalizedScriptRef,
        segments_key: str,
        ingest: IngestResult,
        round: int,
    ) -> DubbedAudio:
        tag_session(spec.aweme_id)
        # The script is what the track is built from; the segments document says how
        # each clip is fitted. They must be the same version.
        doc = DubSegmentsDocument.model_validate_json(
            self._store.get_bytes(segments_key, produced_by="dub_script")
        )
        script_doc = read_script(self._store, script.script_key)
        if doc.script_version != script.version:
            raise ApplicationError(
                "the segments document was made for a different script version",
                type="ScriptVersionMismatch",
                non_retryable=True,
            )
        starts = {s.id: s.start_ms for s in script_doc.segments}
        out_key = work_key(spec.aweme_id, round, "dubbed.wav")
        root = Path(tempfile.mkdtemp(prefix="assemble-"))
        try:
            digest = await run_blocking(self._build, root, doc, starts, ingest, out_key)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        return DubbedAudio(
            audio_key=out_key,
            audio_sha256=digest,
            script_key=script.script_key,
            script_version=script.version,
            segments_key=segments_key,
        )

    def _build(
        self,
        root: Path,
        doc: DubSegmentsDocument,
        starts: dict[int, int],
        ingest: IngestResult,
        out_key: str,
    ) -> str:
        bgm = root / "bgm.wav"
        self._store.download_file(ingest.bgm_key, bgm, produced_by="ingest")
        clips: list[Path] = []
        placements: list[tuple[int, float]] = []
        fetched: dict[str, Path] = {}
        for seg in doc.segments:
            path = fetched.get(seg.tts_key)
            if path is None:
                path = root / f"clip-{len(fetched)}.wav"
                self._store.download_file(seg.tts_key, path, produced_by="synthesize")
                fetched[seg.tts_key] = path
            clips.append(path)
            placements.append((starts[seg.id], seg.stretch_ratio))
        out = root / "dubbed.wav"
        render(
            bgm=bgm,
            clips=clips,
            placements=placements,
            duration_ms=ingest.duration_ms,
            out=out,
            work=root,
            target=self._target,
            peak=self._peak,
        )
        check_length(out, ingest.duration_ms)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        self._store.put_file(out_key, out, content_type="audio/wav")
        return digest

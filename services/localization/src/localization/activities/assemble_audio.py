"""Step 6: the dubbed track.

Every clip is sped (or slowed) by its stretch ratio and placed at its segment's start;
the clips are mixed into one voice track; the background music is ducked under it; over
a sung segment the original vocals are laid back in, and nothing ducks the music there;
the music is set so the voice sits just under it (loud music would bury the voice once
the track is normalised); the result is loudness-normalised in two passes to the
configured target. The output must be as long as the video, because a track that is not
would drift against the picture.

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
from localization.storage.object_store import (
    ArtifactMissingError,
    ObjectStore,
    require_work_key,
    work_key_for,
)

SAMPLE_RATE = 44100
# The track and the video may differ by this much and no more.
LENGTH_TOLERANCE_MS = 50
LOUDNORM_LRA = 11

# Music drops under the voice: heavy compression keyed on the voice track.
DUCK = "threshold=0.03:ratio=10:attack=20:release=300"
# The music is set so the dubbed voice sits this far under the ducked music, measured
# before the final loudness pass (which lifts both by the same amount). A video whose
# music is loud would otherwise bury the voice, because that pass holds the sum constant.
VOICE_UNDER_MUSIC_DB = -0.5
BALANCE_TOLERANCE_DB = 0.1
BALANCE_ROUNDS = 6
MAX_MUSIC_GAIN_DB = 24.0
# The original vocals of a sung segment fade in and out over this long at its edges.
KEEP_FADE_S = 0.06

_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _kept_vocals(source: str, kept: list[tuple[int, int]], seconds: float) -> list[str]:
    """Filters that lay the original vocals (the stream labelled `source`) back in over
    each span of `kept` (start and end in ms), faded at both edges, as one track
    labelled `[keep]`."""
    lines = [f"{source}asplit={len(kept)}" + "".join(f"[o{i}]" for i in range(len(kept)))]
    for i, (start_ms, end_ms) in enumerate(kept):
        length = (end_ms - start_ms) / 1000
        fade = min(KEEP_FADE_S, length / 2)
        lines.append(
            f"[o{i}]atrim=start={start_ms / 1000:.3f}:end={end_ms / 1000:.3f},"
            f"asetpts=PTS-STARTPTS,afade=t=in:d={fade:.3f},"
            f"afade=t=out:st={length - fade:.3f}:d={fade:.3f},adelay={start_ms}:all=1[k{i}]"
        )
    lines.append(
        "".join(f"[k{i}]" for i in range(len(kept)))
        + f"amix=inputs={len(kept)}:normalize=0:duration=longest,"
        f"apad=whole_dur={seconds:.3f}[keep]"
    )
    return lines


def _voice_lines(placements: list[tuple[int, float]], fmt: str, seconds: float) -> list[str]:
    """One line per clip (stretched, delayed to its start) and their mix as `[voice]`,
    padded to the video's length: the ducker ends when its control input does, and a
    track that ended with the last line would cut the music off there."""
    lines = []
    labels = []
    for index, (start_ms, ratio) in enumerate(placements, start=1):
        labels.append(f"[v{index}]")
        lines.append(f"[{index}:a]{fmt},atempo={ratio:.6f},adelay={start_ms}:all=1[v{index}]")
    lines.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest,"
        f"apad=whole_dur={seconds:.3f}[voice]"
    )
    return lines


def build_filter_graph(
    placements: list[tuple[int, float]],
    duration_ms: int,
    kept: list[tuple[int, int]] | None = None,
    under: float = 0.0,
    music_gain_db: float = 0.0,
) -> str:
    """The ffmpeg filter graph. Input 0 is the music; inputs 1.. are the clips, in
    the order of `placements` (start in ms, stretch ratio); the input after them is the
    original vocals when `kept` names spans (start and end in ms) where they are kept.
    Those spans are not in the voice that ducks the music. When `under` is above 0 the
    same vocals also run quietly (at that fraction of their level) under the dubbed
    voice everywhere else: not in the voice that ducks the music, and silent over the
    kept spans, where they already play in full. Pure, so the arithmetic of
    where each clip goes can be tested without ffmpeg. `music_gain_db` lifts or lowers the
    music before it is ducked."""
    seconds = duration_ms / 1000
    fmt = f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo"
    kept = [(a, b) for a, b in kept or [] if b > a]
    gain = f"volume={music_gain_db:.3f}dB," if music_gain_db else ""
    lines = [f"[0:a]{fmt},{gain}apad=whole_dur={seconds:.3f},atrim=end={seconds:.3f}[bgm]"]
    vocals = len(placements) + 1
    if kept and under > 0:
        lines.append(f"[{vocals}:a]{fmt},asplit=2[vk][vu]")
        kept_source, under_source = "[vk]", "[vu]"
    else:
        kept_source = under_source = f"[{vocals}:a]{fmt},"
    if kept:
        lines += _kept_vocals(kept_source, kept, seconds)
    if under > 0:
        quiet = f"{under_source}volume={under:.4f}"
        for start_ms, end_ms in kept:
            quiet += f",volume=0:enable='between(t,{start_ms / 1000:.3f},{end_ms / 1000:.3f})'"
        lines.append(f"{quiet}[under]")
    extras = ("[keep]" if kept else "") + ("[under]" if under > 0 else "")
    voices = 2 + bool(kept) + (under > 0)
    if placements:
        lines += _voice_lines(placements, fmt, seconds)
        lines.append("[voice]asplit=2[vkey][vmix]")
        lines.append(f"[bgm][vkey]sidechaincompress={DUCK}[ducked]")
        # Trimmed, because a line that still overflows at the largest stretch (and is
        # marked `overflow_unresolved`) must lose its tail at the end of the video, not
        # fail the run after the speech is paid for.
        lines.append(
            f"[ducked][vmix]{extras}amix=inputs={voices}:normalize=0:duration=longest,"
            f"atrim=end={seconds:.3f}[mix]"
        )
    elif extras:
        lines.append(
            f"[bgm]{extras}amix=inputs={voices - 1}:normalize=0:duration=longest,"
            f"atrim=end={seconds:.3f}[mix]"
        )
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


_INTEGRATED_RE = re.compile(rb"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+|-?inf) LUFS")


def _integrated_lufs(path: Path) -> float | None:
    """Integrated loudness of a track, or None when it is silent."""
    result = run_ffmpeg(["-i", str(path), "-af", "ebur128", "-f", "null", "-"], loglevel="info")
    found = _INTEGRATED_RE.findall(result.stderr)
    if not found:
        raise PermanentProviderError("ffmpeg: ebur128 did not report a loudness")
    value = float(found[-1])
    return value if value > -70 else None


def _stems_graph(placements: list[tuple[int, float]], duration_ms: int, gain_db: float) -> str:
    """The voice track and the ducked music, each as its own output, with the music
    lifted by `gain_db` first."""
    seconds = duration_ms / 1000
    fmt = f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo"
    gain = f"volume={gain_db:.3f}dB," if gain_db else ""
    return ";\n".join(
        [
            f"[0:a]{fmt},{gain}apad=whole_dur={seconds:.3f},atrim=end={seconds:.3f}[bgm]",
            *_voice_lines(placements, fmt, seconds),
            "[voice]asplit=2[vkey][vout]",
            f"[bgm][vkey]sidechaincompress={DUCK}[dout]",
        ]
    )


def balance_music_gain(
    *,
    bgm: Path,
    clips: list[Path],
    placements: list[tuple[int, float]],
    duration_ms: int,
    work: Path,
) -> float:
    """The gain (dB) to give the music so the dubbed voice sits `VOICE_UNDER_MUSIC_DB`
    under the music as it is heard while the voice speaks. Ducking is not linear in the
    music's level, so this closes in on it in a few rounds. 0 when nothing is spoken or
    either track is silent."""
    if not placements:
        return 0.0
    inputs: list[str] = []
    for path in [bgm, *clips]:
        inputs += ["-i", str(path)]
    graph = work / "balance-graph.txt"
    voice_wav, music_wav = work / "balance-voice.wav", work / "balance-music.wav"
    gain = 0.0
    for _ in range(BALANCE_ROUNDS):
        graph.write_text(_stems_graph(placements, duration_ms, gain))
        run_ffmpeg(
            [
                *inputs,
                "-filter_complex_script", str(graph),
                "-map", "[vout]", "-c:a", "pcm_f32le", str(voice_wav),
                "-map", "[dout]", "-c:a", "pcm_f32le", str(music_wav),
            ]
        )  # fmt: skip
        voice, music = _integrated_lufs(voice_wav), _integrated_lufs(music_wav)
        if voice is None or music is None:
            return 0.0
        error = (voice - music) - VOICE_UNDER_MUSIC_DB
        if abs(error) <= BALANCE_TOLERANCE_DB:
            break
        # Voice too far under the music (error < 0): the music comes down.
        gain = max(-MAX_MUSIC_GAIN_DB, min(MAX_MUSIC_GAIN_DB, gain + error))
    return gain


def music_gain_db(balance_db: float, offset_db: float) -> float:
    """The gain the music gets: what the balance sets, moved by the collection's offset,
    held within the same bounds as the balance itself."""
    return max(-MAX_MUSIC_GAIN_DB, min(MAX_MUSIC_GAIN_DB, balance_db + offset_db))


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
    allow_silence: bool = False,
    vocals: Path | None = None,
    kept: list[tuple[int, int]] | None = None,
    under: float = 0.0,
    music_offset_db: float = 0.0,
) -> None:
    """`vocals` is the original vocals, laid back in over the `kept` spans and, when
    `under` is above 0, quietly under the dubbed voice everywhere else.
    `music_offset_db` (the collection's setting) moves the music from where the
    balance puts it, also when nothing is spoken and the balance gives 0."""
    if kept and vocals is None:
        raise ValueError("kept spans need the original vocals")
    if vocals is None:
        under = 0.0
    music_gain = music_gain_db(
        balance_music_gain(
            bgm=bgm, clips=clips, placements=placements, duration_ms=duration_ms, work=work
        ),
        music_offset_db,
    )
    graph = work / "graph.txt"
    graph.write_text(build_filter_graph(placements, duration_ms, kept, under, music_gain))
    mixed = work / "mix.wav"
    inputs: list[str] = []
    for path in [bgm, *clips, *([vocals] if vocals is not None else [])]:
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
        if allow_silence:
            run_ffmpeg(["-i", str(mixed), "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(out)])
            return
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
    def __init__(
        self,
        store: ObjectStore,
        *,
        loudness_lufs: float,
        true_peak_dbtp: float,
        original_voice_gain: float = 0.0,
    ) -> None:
        self._store = store
        self._under = original_voice_gain
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
        require_work_key(spec, segments_key)
        require_work_key(spec, ingest.bgm_key)
        # The script is what the track is built from; the segments document says how
        # each clip is fitted. They must be the same version.
        doc = DubSegmentsDocument.model_validate_json(
            self._store.get_bytes(segments_key, produced_by="dub_script")
        )
        script_doc = read_script(self._store, script.script_key, spec, script.version)
        if doc.script_version != script.version:
            raise ApplicationError(
                "the segments document was made for a different script version",
                type="ScriptVersionMismatch",
                non_retryable=True,
            )
        if spec.source_snapshot is not None:
            spoken = {s.id: (s.text_vi, s.status, s.reason) for s in doc.segments}
            captions = {s.id: (s.text_vi, s.status, s.reason) for s in script_doc.segments}
            if doc.aweme_id != spec.aweme_id or spoken != captions:
                raise ApplicationError(
                    "dub provenance differs from script",
                    type="ScriptVersionMismatch",
                    non_retryable=True,
                )
        starts = {s.id: s.start_ms for s in script_doc.segments}
        kept = [(s.start_ms, s.end_ms) for s in script_doc.segments if s.keep_original]
        if kept:
            require_work_key(spec, ingest.vocals_key)
        out_key = work_key_for(spec, round, "dubbed.wav")
        root = Path(tempfile.mkdtemp(prefix="assemble-"))
        try:
            offset = spec.collection.bgm_offset_db if spec.collection else None
            digest = await run_blocking(
                self._build, root, doc, starts, ingest, out_key, kept, offset or 0.0
            )
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
        kept: list[tuple[int, int]] | None = None,
        music_offset_db: float = 0.0,
    ) -> str:
        bgm = root / "bgm.wav"
        omitted = any(s.status == "unresolved" for s in doc.segments)
        try:
            self._store.download_file(ingest.bgm_key, bgm, produced_by="ingest")
        except ArtifactMissingError:
            if not omitted:
                raise
            run_ffmpeg(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
                    "-t",
                    f"{ingest.duration_ms / 1000:.3f}",
                    "-c:a",
                    "pcm_s16le",
                    str(bgm),
                ]
            )
        clips: list[Path] = []
        placements: list[tuple[int, float]] = []
        fetched: dict[str, Path] = {}
        for seg in doc.segments:
            if seg.tts_key is None:
                # Omitted, or sung: nothing is spoken over it.
                continue
            path = fetched.get(seg.tts_key)
            if path is None:
                path = root / f"clip-{len(fetched)}.wav"
                self._store.download_file(seg.tts_key, path, produced_by="synthesize")
                fetched[seg.tts_key] = path
            clips.append(path)
            placements.append((starts[seg.id], seg.stretch_ratio))
        vocals = None
        if kept or self._under > 0:
            vocals = root / "vocals.wav"
            try:
                self._store.download_file(ingest.vocals_key, vocals, produced_by="ingest")
            except ArtifactMissingError:
                # Only the sung spans cannot do without them; the quiet layer can.
                if kept:
                    raise
                vocals = None
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
            allow_silence=bool(doc.segments)
            and all(s.status == "unresolved" for s in doc.segments),
            vocals=vocals,
            kept=kept,
            under=self._under,
            music_offset_db=music_offset_db,
        )
        check_length(out, ingest.duration_ms)
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        self._store.put_file(out_key, out, content_type="audio/wav")
        return digest

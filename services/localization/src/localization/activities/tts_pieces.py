"""A line is read as short pieces and the pieces are joined back into one clip.

The voice model skips words when it reads a long line in one request: a 150 to 230
character line came back without "bảy mươi", without a whole clause, or without its
first sentence. So a line is cut at its sentence ends, a sentence longer than
MAX_PIECE_CHARS is cut again at its clause punctuation, each piece is read on its
own, and the pieces are joined with a short pause.

A cut is only ever made at punctuation followed by a space, so a number ("4.6",
"1,5", "3:30"), a number in words ("bốn phẩy sáu") and a name are never cut. When it
is unclear whether a full stop ends a sentence, the line is not cut there: a piece
that is a little long costs less than a name read in two halves.
"""

import array
import io
import sys
import tempfile
import wave
from pathlib import Path

from localization.activities.ffmpeg import run_ffmpeg
from localization.text.pieces import (  # noqa: F401 - where a line is cut, re-exported
    CLAUSE_PAUSE_MS,
    MAX_PIECE_CHARS,
    SENTENCE_PAUSE_MS,
    Piece,
    split_line,
)

# Silence where two pieces meet is trimmed to this margin, so the pause is the one
# chosen in `text.pieces` and not the pause plus whatever the voice left at both ends. The
# margin keeps a soft consonant at the edge of a word (an "s", a breath) intact.
TRIM_MARGIN_MS = 30
# Below this share of full scale (about -40 dBFS) a sample is silence.
SILENCE_THRESHOLD = 0.01

# The pieces of one line are decoded to one format before they are joined: mono,
# 16-bit, at the rate the voice model speaks at.
JOIN_SAMPLE_RATE = 24000


def _decode(wav: bytes, root: Path, index: int) -> array.array:
    """The samples of one piece, in the join format. Any WAV the provider sends is
    accepted; one ffmpeg cannot read fails the join."""
    source, raw = root / f"piece-{index}.wav", root / f"piece-{index}.raw"
    source.write_bytes(wav)
    run_ffmpeg(
        ["-i", str(source), "-f", "s16le", "-ac", "1", "-ar", str(JOIN_SAMPLE_RATE), str(raw)]
    )
    samples = array.array("h")
    samples.frombytes(raw.read_bytes())
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def _speech_bounds(samples: array.array) -> tuple[int, int]:
    """Where the sound starts and ends, widened by the margin. A piece that is all
    silence is kept whole rather than made to vanish."""
    threshold = SILENCE_THRESHOLD * 32767
    first = next((i for i, s in enumerate(samples) if abs(s) >= threshold), None)
    if first is None:
        return 0, len(samples)
    last = next(i for i in range(len(samples) - 1, -1, -1) if abs(samples[i]) >= threshold)
    margin = JOIN_SAMPLE_RATE * TRIM_MARGIN_MS // 1000
    return max(0, first - margin), min(len(samples), last + 1 + margin)


def join_pieces(wavs: list[bytes], pauses_ms: list[int]) -> bytes:
    """One WAV of `wavs` in order, `pauses_ms[i]` of silence after the i-th. Silence
    is trimmed only where two pieces meet: the start of the first piece and the end
    of the last are left as the provider made them. Blocking: ffmpeg runs once per
    piece."""
    if len(pauses_ms) != len(wavs) - 1:
        raise ValueError("a pause is needed between every two pieces and nowhere else")
    joined = array.array("h")
    with tempfile.TemporaryDirectory(prefix="pieces-") as tmp:
        for index, wav in enumerate(wavs):
            samples = _decode(wav, Path(tmp), index)
            start, end = _speech_bounds(samples)
            if index == 0:
                start = 0
            if index == len(wavs) - 1:
                end = len(samples)
            joined.extend(samples[start:end])
            if index < len(pauses_ms):
                joined.extend([0] * (JOIN_SAMPLE_RATE * pauses_ms[index] // 1000))
    if sys.byteorder == "big":
        joined.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(JOIN_SAMPLE_RATE)
        out.writeframes(joined.tobytes())
    return buffer.getvalue()

"""A long line is read as short pieces: where it is cut, and how the pieces are
put back together into one clip."""

import io
import wave

import pytest

from localization.activities.ffmpeg import probe_duration_ms
from localization.activities.tts_pieces import (
    CLAUSE_PAUSE_MS,
    JOIN_SAMPLE_RATE,
    MAX_PIECE_CHARS,
    SENTENCE_PAUSE_MS,
    TRIM_MARGIN_MS,
    Piece,
    join_pieces,
    split_line,
)
from tests.support.tts_fakes import make_wav

LINE_3 = (
    "Dịu nhẹ, dễ hấp thu, không kích ứng, còn kết hợp thêm peptide hemoglobin, "
    "anthocyanin và vitamin C … đã có bốn phẩy sáu miligam sắt."
)


def texts(line: str) -> list[str]:
    return [p.text for p in split_line(line)]


# --- where a line is cut ---------------------------------------------------------


def test_a_short_line_of_one_sentence_is_one_piece() -> None:
    line = "Cứ một trăm bạn nữ thì bảy mươi bạn thiếu sắt."
    assert split_line(line) == [Piece(line, 0)]


def test_a_line_is_cut_at_every_sentence_end() -> None:
    line = "Sáu miligam sắt. Mỗi ngày uống ba viên là vừa đủ! Bạn đã thử chưa?"
    assert split_line(line) == [
        Piece("Sáu miligam sắt.", SENTENCE_PAUSE_MS),
        Piece("Mỗi ngày uống ba viên là vừa đủ!", SENTENCE_PAUSE_MS),
        Piece("Bạn đã thử chưa?", 0),
    ]


def test_a_long_sentence_is_cut_at_a_comma_into_pieces_within_the_maximum() -> None:
    pieces = split_line(LINE_3)
    assert len(LINE_3) > MAX_PIECE_CHARS
    assert [p.text for p in pieces] == [
        "Dịu nhẹ, dễ hấp thu, không kích ứng, còn kết hợp thêm peptide hemoglobin,",
        "anthocyanin và vitamin C … đã có bốn phẩy sáu miligam sắt.",
    ]
    assert pieces[0].pause_after_ms == CLAUSE_PAUSE_MS
    assert pieces[-1].pause_after_ms == 0
    assert all(len(p.text) <= MAX_PIECE_CHARS for p in pieces)


@pytest.mark.parametrize(
    "number",
    ["4.6", "1,5", "10.000", "bốn phẩy sáu", "một trăm hai mươi lăm", "3:30"],
)
def test_a_number_is_never_cut(number: str) -> None:
    """The number sits where a greedy cut by length would fall, in a sentence long
    enough that it must be cut somewhere."""
    head = "Theo nghiên cứu mới nhất của các chuyên gia dinh dưỡng hàng đầu thì cứ mỗi"
    line = f"{head} {number} viên, bạn sẽ bổ sung đủ lượng sắt cần thiết cho cả ngày dài."
    pieces = texts(line)
    assert len(pieces) > 1
    assert sum(number in p for p in pieces) == 1


@pytest.mark.parametrize(
    "line",
    [
        "Sản phẩm này được bán rộng rãi ở TP. Hồ Chí Minh và Hà Nội.",
        "Lời khuyên này đến từ Dr. Nguyễn, một bác sĩ dinh dưỡng.",
        "Rau, củ, quả v.v. đều có sắt.",
        "Vitamin C … giúp hấp thu sắt tốt hơn.",
    ],
)
def test_an_abbreviation_or_a_pause_mid_sentence_is_not_a_sentence_end(line: str) -> None:
    assert texts(line) == [line]


def test_a_clause_longer_than_the_maximum_is_kept_whole_rather_than_cut_mid_phrase() -> None:
    clause = "Sắt " + "rất cần cho cơ thể " * 6 + "của chúng ta"
    line = f"{clause}, vậy nên hãy bổ sung mỗi ngày."
    assert len(clause) > MAX_PIECE_CHARS
    assert texts(line) == [f"{clause},", "vậy nên hãy bổ sung mỗi ngày."]


def test_the_pieces_put_back_together_are_the_line() -> None:
    line = "  Sáu miligam sắt.   Mỗi ngày uống ba viên,\n là vừa đủ. " + LINE_3
    assert " ".join(texts(line)) == " ".join(line.split())


@pytest.mark.parametrize("line", ["", "   ", "\n"])
def test_a_blank_line_has_no_pieces(line: str) -> None:
    assert split_line(line) == []


# --- how the pieces are joined ----------------------------------------------------


def _pcm_seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as audio:
        return audio.getnframes() / audio.getframerate()


def _padded(silence: float, tone: float) -> bytes:
    """`silence` seconds of silence, `tone` seconds of tone, `silence` seconds again:
    what a TTS clip looks like around its speech."""
    quiet = make_wav(silence, amplitude=0.0)
    loud = make_wav(tone)
    frames = b""
    for part in (quiet, loud, quiet):
        with wave.open(io.BytesIO(part)) as audio:
            frames += audio.readframes(audio.getnframes())
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(24000)
        out.writeframes(frames)
    return buffer.getvalue()


def test_the_joined_clip_lasts_the_pieces_plus_the_pauses() -> None:
    pieces = [make_wav(0.8), make_wav(1.3), make_wav(0.5)]
    joined = join_pieces(pieces, [SENTENCE_PAUSE_MS, CLAUSE_PAUSE_MS])
    expected_ms = 800 + 1300 + 500 + SENTENCE_PAUSE_MS + CLAUSE_PAUSE_MS
    assert _pcm_seconds(joined) * 1000 == pytest.approx(expected_ms, abs=1)


def test_the_joined_clip_measures_the_same_through_ffprobe(tmp_path) -> None:
    """`duration_ms` is what the fit and the stretch are computed from, so the
    container must say what the samples say."""
    joined = join_pieces([make_wav(0.8), make_wav(1.3)], [SENTENCE_PAUSE_MS])
    path = tmp_path / "joined.wav"
    path.write_bytes(joined)
    assert probe_duration_ms(path) == pytest.approx(800 + 1300 + SENTENCE_PAUSE_MS, abs=2)


def test_silence_between_pieces_is_trimmed_so_the_pauses_do_not_pile_up() -> None:
    """Only the edges that meet another piece are trimmed, down to a short margin:
    the line starts and ends as the provider made it, like a line of one piece."""
    pieces = [_padded(0.4, 1.0), _padded(0.4, 0.6)]
    joined = join_pieces(pieces, [SENTENCE_PAUSE_MS])
    kept_inside = 2 * TRIM_MARGIN_MS
    expected_ms = 400 + 1000 + kept_inside + SENTENCE_PAUSE_MS + 600 + 400
    assert _pcm_seconds(joined) * 1000 == pytest.approx(expected_ms, abs=2)


def test_a_piece_that_is_all_silence_is_kept_rather_than_made_to_vanish() -> None:
    pieces = [make_wav(0.5), make_wav(0.3, amplitude=0.0), make_wav(0.4)]
    joined = join_pieces(pieces, [CLAUSE_PAUSE_MS, CLAUSE_PAUSE_MS])
    expected_ms = 500 + CLAUSE_PAUSE_MS + 300 + CLAUSE_PAUSE_MS + 400
    assert _pcm_seconds(joined) * 1000 == pytest.approx(expected_ms, abs=1)


def test_the_joined_clip_is_mono_16_bit_at_the_join_rate() -> None:
    joined = join_pieces([make_wav(0.2), make_wav(0.2)], [CLAUSE_PAUSE_MS])
    with wave.open(io.BytesIO(joined)) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
            1,
            2,
            JOIN_SAMPLE_RATE,
        )


def test_a_piece_that_is_not_audio_fails_the_join() -> None:
    from localization.providers.errors import PermanentProviderError

    with pytest.raises(PermanentProviderError):
        join_pieces([make_wav(0.2), b"not a wav"], [CLAUSE_PAUSE_MS])


def test_the_pauses_must_be_one_fewer_than_the_pieces() -> None:
    with pytest.raises(ValueError):
        join_pieces([make_wav(0.2), make_wav(0.2)], [])

"""How a line is cut into the pieces it is read in. Pure: the workflow sizes a synthesis
batch's time by the pieces of its lines, and the synthesis reads each piece on its own
(see `activities.tts_pieces`).

A cut is only ever made at punctuation followed by a space, so a number ("4.6",
"1,5", "3:30"), a number in words ("bốn phẩy sáu") and a name are never cut. When it
is unclear whether a full stop ends a sentence, the line is not cut there: a piece
that is a little long costs less than a name read in two halves.
"""

import re
from dataclasses import dataclass

# About five seconds of Vietnamese speech: half the shortest line seen to lose words,
# and long enough that most clauses fit whole and a long line becomes two or three
# pieces rather than one per comma.
MAX_PIECE_CHARS = 80
# A line whose clip lost words is read again with every clause on its own.
CLAUSE_PIECE_CHARS = 1

# The pause put between two pieces: a full breath after a sentence, a short one
# after a clause, close to what the voice leaves there when it reads the line whole.
SENTENCE_PAUSE_MS = 200
CLAUSE_PAUSE_MS = 120

_SENTENCE_END = re.compile(r"[.!?…。！？]+[\"'”’)\]]*\s+")
_CLAUSE_END = re.compile(r"[,;:，；：]+[\"'”’)\]]*\s+")
# A word before a full stop that is a title or an initialism: "TP.", "Dr.", "PGS."
_ABBREVIATION = re.compile(r"(?:^|\s)(?:[A-ZĐ]{2,3}|Dr|Mr|Mrs|Ms|St|ThS|Tp)\.$")


@dataclass(frozen=True)
class Piece:
    """One request's worth of a line and the pause that follows it (0 for the last)."""

    text: str
    pause_after_ms: int


def _is_sentence_end(text: str, match: re.Match[str]) -> bool:
    following = text[match.end() : match.end() + 1]
    if following.islower():
        # "… đã có", "v.v. đều": the sentence goes on.
        return False
    return not _ABBREVIATION.search(text[: match.start() + 1])


def _cut(text: str, pattern: re.Pattern[str], keep=lambda _text, _match: True) -> list[str]:
    parts: list[str] = []
    start = 0
    for match in pattern.finditer(text):
        if keep(text, match):
            parts.append(text[start : match.end()].strip())
            start = match.end()
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _pack(clauses: list[str], max_chars: int) -> list[str]:
    """Consecutive clauses joined into pieces of at most `max_chars`. A clause
    longer than that stays whole: there is no safe place to cut it."""
    pieces: list[str] = []
    for clause in clauses:
        if pieces and len(pieces[-1]) + 1 + len(clause) <= max_chars:
            pieces[-1] = f"{pieces[-1]} {clause}"
        else:
            pieces.append(clause)
    return pieces


def split_line(text: str, max_chars: int = MAX_PIECE_CHARS) -> list[Piece]:
    """The pieces `text` is read in, in order. Whitespace inside the line is
    collapsed; the pieces joined with single spaces are the line. A sentence longer
    than `max_chars` is cut at its clauses (every clause alone, for CLAUSE_PIECE_CHARS)."""
    line = " ".join(text.split())
    pieces: list[Piece] = []
    for sentence in _cut(line, _SENTENCE_END, _is_sentence_end):
        parts = [sentence]
        if len(sentence) > max_chars:
            parts = _pack(_cut(sentence, _CLAUSE_END), max_chars)
        pieces.extend(Piece(part, CLAUSE_PAUSE_MS) for part in parts)
        pieces[-1] = Piece(pieces[-1].text, SENTENCE_PAUSE_MS)
    if pieces:
        pieces[-1] = Piece(pieces[-1].text, 0)
    return pieces

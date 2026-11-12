"""Which words of a line a voice did not say, judged from what a speech-to-text model heard.

The voice model sometimes drops a whole clause of a short line ("dáng thanh mảnh" from
"Mặt thì xinh xắn, dáng thanh mảnh, sao nhảy khỏe thế?"), and a clip that lost a fifth of
its words is as long as a clip that lost none at a slightly faster pace, so its length
cannot tell. What was heard can.

Words are compared without tone marks and case: the transcription model may spell a tone
differently from the script, and that is not a lost word. Numbers are compared as the
words that read them, since the voice says "ba mươi" and the script has "30".
Pure: no network, no model.
"""

import re
import unicodedata

from localization.text.vi_syllables import numbers_to_words

# A line is judged lost when it is missing at least this many words and at least this
# share of them. One missing word in a long line is within what a transcription model gets
# wrong by itself (a name, a foreign word said the Vietnamese way); two or more, or a fifth
# of a short line, is a clause gone.
MIN_LOST_WORDS = 2
MIN_LOST_SHARE = 0.2

_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _key(word: str) -> str:
    decomposed = unicodedata.normalize("NFD", word.lower().replace("đ", "d"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def words_of(text: str, *, spoken_numbers: bool) -> list[str]:
    """The comparable words of `text`, in order. With `spoken_numbers`, digits are first
    rewritten as the words that read them (the script side); a transcription that
    already writes numbers in words needs no rewrite."""
    if spoken_numbers:
        text = numbers_to_words(text)
    return [_key(w) for w in _WORD.findall(text)]


def lost_words(expected: str, heard: str) -> list[str]:
    """The words of `expected` that have no place in `heard`, in the order of `expected`
    (a longest common subsequence: a word said twice by the voice does not hide one that
    was not said)."""
    want = words_of(expected, spoken_numbers=True)
    got = words_of(heard, spoken_numbers=True)
    # table[i][j]: how many words of want[i:] line up with got[j:].
    table = [[0] * (len(got) + 1) for _ in range(len(want) + 1)]
    for i in range(len(want) - 1, -1, -1):
        for j in range(len(got) - 1, -1, -1):
            if want[i] == got[j]:
                table[i][j] = table[i + 1][j + 1] + 1
            else:
                table[i][j] = max(table[i + 1][j], table[i][j + 1])
    lost: list[str] = []
    i = j = 0
    while i < len(want):
        if j < len(got) and want[i] == got[j]:
            i += 1
            j += 1
        elif j < len(got) and table[i][j + 1] >= table[i + 1][j]:
            j += 1
        else:
            lost.append(want[i])
            i += 1
    return lost


def is_lost(expected: str, lost: list[str]) -> bool:
    """Whether `lost` (from `lost_words`) is enough to call a clip of `expected` wrong."""
    want = words_of(expected, spoken_numbers=True)
    return len(lost) >= MIN_LOST_WORDS and len(lost) >= MIN_LOST_SHARE * max(len(want), 1)


def lost_too_many(expected: str, heard: str) -> list[str]:
    """The lost words when they are enough to call the clip wrong, else an empty list."""
    lost = lost_words(expected, heard)
    return lost if is_lost(expected, lost) else []

"""Collapsing a word or letter said three or more times in a row to a single one.

A scream transcribed as sixty `啊` reached the Localizer as written, and the model,
answering without reasoning, wrote "Á á á …" until its token cap. The repetition carries
no meaning a translation needs, so the transcript the Localizer reads keeps one of it.
Pure and deterministic, so activities and tools give the same text for the same input.
"""

import re

# A letter (not a digit: "1000" is a number) repeated at least three times: "啊啊啊",
# "ㅋㅋㅋ", "Aaaah". Case is ignored so "Aaa" is one run.
_LETTER_RUN = re.compile(r"([^\W\d_])\1{2,}", re.IGNORECASE)
# A word repeated at least three times, apart by spaces or punctuation: "á á á",
# "no, no, no!". The separators after the first word go with the repeats.
_WORD_RUN = re.compile(r"\b(\w+)(?:[\W_]+\1\b){2,}", re.IGNORECASE)


def collapse_repeats(text: str) -> str:
    """`text` with each letter or word repeated three or more times in a row kept once."""
    return _WORD_RUN.sub(r"\1", _LETTER_RUN.sub(r"\1", text))

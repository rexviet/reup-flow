"""Characters a Vietnamese caption cannot show. Pure, so both workflow code (the
Localizer's answer check) and activities can use it.

The caption font covers Vietnamese, digits and ordinary punctuation; a character it
lacks would be burned into the video as an empty box, and the layout stage refuses
such a caption for good, after the dubbing has been paid for. This check answers the
same question earlier and without the font file, by what a character IS: a letter of
another script (Chinese, Japanese, Korean, Cyrillic, ...), a pictograph or emoji, an
arrow or other symbol, full-width or CJK punctuation. It is a cheap first filter,
not the font's own verdict, so the layout check stays.
"""

import unicodedata

# Symbols that belong in a spoken caption ("30%", "2 + 3", "5 × 4") and that the
# caption font draws.
_ALLOWED_SYMBOLS = frozenset("+-=<>%±×÷~°")
# Blocks whose punctuation and symbols the caption font does not draw.
_CJK_PUNCTUATION = range(0x3000, 0x3040)
_HALF_AND_FULLWIDTH = range(0xFF00, 0xFFF0)


def _is_latin_letter(char: str) -> bool:
    return unicodedata.name(char, "").startswith("LATIN")


def foreign_chars(text: str) -> list[str]:
    """The characters of `text` a Vietnamese caption cannot show, in order, without
    repeats."""
    found: dict[str, None] = {}
    for char in unicodedata.normalize("NFC", text):
        code = ord(char)
        category = unicodedata.category(char)
        if code in _CJK_PUNCTUATION or code in _HALF_AND_FULLWIDTH:
            found[char] = None
        elif category.startswith("L"):
            if not _is_latin_letter(char):
                found[char] = None
        elif category in ("So", "Sk") or category in ("Cs", "Co", "Cn"):
            found[char] = None
        elif category in ("Sm", "Sc"):
            # Currency signs (₫, $, €) are drawn; maths symbols only when spoken.
            if category == "Sm" and char not in _ALLOWED_SYMBOLS:
                found[char] = None
    return list(found)


def foreign_chars_note(chars: list[str]) -> str:
    shown = " ".join(f"{c!r}" for c in chars[:10])
    return f"{shown}{' ...' if len(chars) > 10 else ''}"

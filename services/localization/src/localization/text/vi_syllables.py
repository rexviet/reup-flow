"""How many syllables a Vietnamese line takes to read aloud.

Vietnamese writes one syllable per space-separated token, so the count is the
number of tokens, after two corrections that matter for dubbing:

- digits are spoken as words ("2024" is "hai nghìn lẻ hai mươi bốn", seven
  syllables, not one token), including years, percentages, money and thousands
  separators;
- Latin-script words (iPhone, TikTok) are not Vietnamese syllables; they are
  estimated by their vowel groups, and short all-capital acronyms are spelled out
  letter by letter.

`estimate_ms` turns a count into a duration with a speaking rate measured from the
voice itself.
"""

import re

from num2words import num2words

# 1.234.567 or 1,234,567: a separator followed by exactly three digits, repeated.
_GROUPED_RE = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?![\d])")
_DECIMAL_RE = re.compile(r"\d+[.,]\d+")
_NUMBER_RE = re.compile(r"\d+")
_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:%|％)")
_MONEY_PREFIX_RE = re.compile(r"([$¥￥€£])\s*(\d[\d.,]*)")
_MONEY_SUFFIX_RE = re.compile(r"(\d[\d.,]*)\s*([$¥￥€£₫đ])(?![A-Za-zÀ-ỹ])")
_THOUSAND_K_RE = re.compile(r"(\d+)\s*[kK](?![A-Za-z])")
# "-5" after a space or at the start is minus five: "âm năm".
_NEGATIVE_RE = re.compile(r"(?<![\w])-(?=\d)")
# Currency sign -> how it is read, as words to count.
_CURRENCY_WORDS = {
    "$": "đô la",
    "¥": "nhân dân tệ",
    "￥": "nhân dân tệ",
    "€": "ơ rô",
    "£": "bảng",
    "₫": "đồng",
    "đ": "đồng",
}
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_CJK_RE = re.compile(r"[　-鿿＀-￯]")
_LATIN_ONLY_RE = re.compile(r"[A-Za-z]+")
_VOWEL_GROUP_RE = re.compile(r"[aeiouy]+", re.IGNORECASE)
# Longest all-capital word still read letter by letter (AI, USB, VIP).
_ACRONYM_MAX = 4


def _spell_integer(digits: str) -> str:
    if len(digits) > 1 and digits.startswith("0"):
        # A code or a number with leading zeros ("007") is read digit by digit.
        return " ".join(num2words(int(d), lang="vi") for d in digits)
    try:
        return num2words(int(digits), lang="vi")
    except (OverflowError, ValueError, NotImplementedError):
        return " ".join(num2words(int(d), lang="vi") for d in digits)


def _spell_decimal(text: str) -> str:
    whole, _, fraction = re.split(r"([.,])", text, maxsplit=1)
    digits = " ".join(num2words(int(d), lang="vi") for d in fraction)
    return f"{_spell_integer(whole)} phẩy {digits}"


def _spell_amount(text: str) -> str:
    """`text` is digits with optional thousands separators or a decimal part."""
    if _GROUPED_RE.fullmatch(text):
        return _spell_integer(re.sub(r"[.,]", "", text))
    if _DECIMAL_RE.fullmatch(text):
        return _spell_decimal(text)
    digits = re.sub(r"\D", "", text)
    return _spell_integer(digits) if digits else ""


def numbers_to_words(text: str) -> str:
    """Rewrites every number in `text` as the Vietnamese words that read it."""
    text = _NEGATIVE_RE.sub("âm ", text)
    text = _PERCENT_RE.sub(lambda m: f" {_spell_amount(m.group(1))} phần trăm ", text)
    text = _MONEY_PREFIX_RE.sub(
        lambda m: f" {_spell_amount(m.group(2).rstrip('.,'))} {_CURRENCY_WORDS[m.group(1)]} ", text
    )
    text = _MONEY_SUFFIX_RE.sub(
        lambda m: f" {_spell_amount(m.group(1).rstrip('.,'))} {_CURRENCY_WORDS[m.group(2)]} ",
        text,
    )
    text = _THOUSAND_K_RE.sub(lambda m: f" {_spell_integer(m.group(1))} nghìn ", text)
    text = _GROUPED_RE.sub(lambda m: f" {_spell_amount(m.group(0))} ", text)
    text = _DECIMAL_RE.sub(lambda m: f" {_spell_decimal(m.group(0))} ", text)
    return _NUMBER_RE.sub(lambda m: f" {_spell_integer(m.group(0))} ", text)


def _latin_syllables(word: str) -> int:
    """Estimated syllables of a word written in Latin letters but not Vietnamese."""
    if word.isupper() and len(word) <= _ACRONYM_MAX:
        return len(word)
    groups = len(_VOWEL_GROUP_RE.findall(word))
    # A final "e" is silent in "iPhone" but not after "l" ("Apple") or when it is the
    # word's only vowel.
    if groups > 1 and word.lower().endswith("e") and not word.lower().endswith(("le", "ee")):
        groups -= 1
    return max(1, groups)


def count_syllables(text: str) -> int:
    """Syllables read aloud for `text`, with numbers and Latin words accounted for."""
    spoken = numbers_to_words(text)
    total = 0
    for match in _TOKEN_RE.finditer(spoken):
        token = match.group(0)
        cjk = len(_CJK_RE.findall(token))
        if cjk:
            # One syllable per Chinese character; any Latin letters around them are
            # counted as a word of their own.
            rest = _CJK_RE.sub(" ", token)
            total += cjk + sum(_latin_syllables(w) for w in _LATIN_ONLY_RE.findall(rest))
        elif _LATIN_ONLY_RE.fullmatch(token) and _is_foreign(token):
            total += _latin_syllables(token)
        else:
            total += 1
    return total


# Plain ASCII words are foreign in a Vietnamese line; a Vietnamese syllable carries a
# tone mark or a letter outside ASCII unless it is a short toneless syllable (ba, mua,
# nha, ...), which is told from a foreign word by its shape: at most four letters and
# one vowel group.
_TONELESS_SYLLABLE_RE = re.compile(
    r"(?:ngh|ng|nh|ch|gh|gi|kh|ph|qu|th|tr|[bcdghklmnpqrstvx])?[aeiouy]{1,3}"
    r"(?:ng|nh|ch|[cmnpt])?",
    re.IGNORECASE,
)


def _is_foreign(token: str) -> bool:
    if token.isupper() and len(token) <= _ACRONYM_MAX:
        return True
    if len(token) <= 6 and _TONELESS_SYLLABLE_RE.fullmatch(token) is not None:
        return False
    return True


def estimate_ms(text: str, syllables_per_sec: float) -> int:
    """Time to read `text` at `syllables_per_sec`, in milliseconds."""
    if syllables_per_sec <= 0:
        raise ValueError("syllables_per_sec must be positive")
    return round(count_syllables(text) / syllables_per_sec * 1000)

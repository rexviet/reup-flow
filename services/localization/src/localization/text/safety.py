"""Spotting contact details in text: URLs, domain names, e-mail addresses and phone
numbers. Pure and deterministic (no model), so workflow code and activities can
both use it.

The question it answers: does a Vietnamese line mention a way to reach someone
that the Chinese source line did not? Subtitles and speech are untrusted input to
the Localizer, and a line that sends viewers to a shop is the harm to catch.
"""

import re
import unicodedata

# Characters that carry no width but split a token for a reader (zero-width space,
# joiners, soft hyphen, BOM). Removed before matching so "shop-xyz.v​n" is seen
# as written.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))

# Top-level domains worth flagging. A closed list on purpose: "xong.Nhưng" (a missing
# space after a full stop) must not look like a domain, and an open `[a-z]{2,}`
# would flag it.
_TLDS = (
    "com|net|org|edu|gov|info|biz|vn|cn|hk|tw|jp|kr|sg|uk|us|ru|de|fr|io|co|me|tv|cc|ly|gl|"
    "ai|app|dev|xyz|top|site|online|shop|store|link|vip|club|live|life|fun|pro|ink|wang|"
    "icu|cyou|work|click|website|tech|page|bio|asia|vc|to|be|im"
)

_URL_RE = re.compile(r"(?:https?|ftp)://\S+|\bwww\.\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    rf"(?<![a-z0-9@-])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_TLDS})(?![a-z0-9-])",
    re.IGNORECASE,
)
# A domain read aloud or dodged: "shop xyz chấm vn", "shop xyz dot com".
_SPOKEN_DOMAIN_RE = re.compile(rf"\b[a-z0-9-]+\s*(?:chấm|dot)\s*(?:{_TLDS})\b", re.IGNORECASE)
# Seven or more digits, which may be split by spaces, dots, dashes or brackets, with
# an optional leading +. Shorter runs (a year, a price, "3.5") are not phone numbers.
# A run straight after "1." or "1," is the tail of a number, but one after a bare
# comma is not: NFKC turns the full-width "，" before a number into ",".
_PHONE_RE = re.compile(r"(?<!\d)(?<!\d[.,])\+?\(?\d(?:[\s().-]?\d){6,}(?![\d])")
# 1.500.000 or 1,000,000: a price or a count written with thousands separators.
_GROUPED_NUMBER_RE = re.compile(r"[1-9]\d{0,2}(?:[.,]\d{3})+")


def normalise(text: str) -> str:
    """NFKC (full-width letters and dots become ASCII), no invisible characters."""
    return unicodedata.normalize("NFKC", text).translate(_INVISIBLE)


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdecimal())


def find_contacts(text: str) -> list[str]:
    """Every URL, domain, e-mail address and phone number in `text`, as written
    (after normalisation), in order of appearance and without repeats."""
    clean = normalise(text)
    found: list[tuple[int, str]] = []
    covered: list[tuple[int, int]] = []

    def collect(pattern: re.Pattern[str]) -> None:
        for match in pattern.finditer(clean):
            if any(start <= match.start() < end for start, end in covered):
                continue
            if pattern is _PHONE_RE and _GROUPED_NUMBER_RE.fullmatch(match.group(0)):
                continue
            covered.append((match.start(), match.end()))
            found.append((match.start(), match.group(0).rstrip(".,;:!?)\"'")))

    for pattern in (_URL_RE, _EMAIL_RE, _DOMAIN_RE, _SPOKEN_DOMAIN_RE, _PHONE_RE):
        collect(pattern)
    ordered: list[str] = []
    for _, value in sorted(found):
        if value not in ordered:
            ordered.append(value)
    return ordered


def _key(contact: str) -> str:
    """What makes two spellings of one contact equal: phone numbers by their digits,
    everything else by lower-cased text without spaces."""
    digits = _digits(contact)
    if len(digits) >= 7 and digits == "".join(ch for ch in contact if ch.isdecimal()):
        stripped = re.sub(r"[\s().+-]", "", contact)
        if stripped.isdecimal():
            return digits
    return re.sub(r"\s+", "", contact).casefold()


def contacts_missing_from_source(text_vi: str, src_zh: str) -> list[str]:
    """The contacts in `text_vi` that the source line does not carry. A phone
    number counts as present when the source has the same digits, however they
    are spaced; any other contact when the source has the same text."""
    source = normalise(src_zh)
    source_compact = re.sub(r"\s+", "", source).casefold()
    source_digits = _digits(source)
    missing: list[str] = []
    for contact in find_contacts(text_vi):
        key = _key(contact)
        if key.isdecimal():
            present = key in source_digits
        else:
            present = key in source_compact
        if not present:
            missing.append(contact)
    return missing

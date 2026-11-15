"""Whether a Vietnamese line carries a number of its source.

A number counts as carried when the line has the same value, written in digits
("4,6", "4.6", "1.500") or spelled in Vietnamese words ("bốn phẩy sáu", "hai mươi mốt",
"một trăm linh năm", "một nghìn rưỡi"). The value is compared, not the text, so
"bảy mươi lăm" does not carry 70 and "20" does not carry 2.

Two idioms that name one number are read too: "đôi mươi" is 20 and a digit word before
"chục" counts tens ("một chục" 10, "ba chục nghìn" 30 thousand). A word that names no
exact number is not: "đôi" alone is a pair, "chục", "vài chục" and "hàng chục" are vague
counts, and "tá" (a dozen) is not read since it is also a syllable of other words ("đại
tá", "tá điền") and "một tá" often means only "a lot".

The number of a source is read whole: "4.6" is one number, not 4 and 6. A source number
followed by 万 or 亿 is also carried by its full value ("3万" by "ba mươi nghìn"), and a
number said as a count of thousands or millions keeps its count ("30元" by "ba mươi
nghìn"). A percent sign or a unit is not required: "50%" is carried by "năm mươi".
"""

import re
import unicodedata
from decimal import Decimal

# Digits with "." or "," between groups: "4.6", "4,6", "1.500", "1,234,567".
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
_GROUPED = re.compile(r"\d{1,3}(?:([.,])\d{3})(?:\1\d{3})*")
_SOURCE_NUMBER = re.compile(rf"({_DIGITS.pattern})\s*([万萬亿億])?")
_SOURCE_SCALES = {"万": 10**4, "萬": 10**4, "亿": 10**8, "億": 10**8}
_LINE_TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")

# Digit words that can start a number or stand alone.
_UNITS = {
    "không": 0,
    "một": 1,
    "hai": 2,
    "ba": 3,
    "bốn": 4,
    "năm": 5,
    "sáu": 6,
    "bảy": 7,
    "bẩy": 7,
    "tám": 8,
    "chín": 9,
}
# The last digit after "mười"/"mươi": "mốt" (1), "tư" (4) and "lăm" (5) are only read
# there, and "năm" is not, since "hai mươi năm" is twenty years.
_LAST_UNITS = {k: v for k, v in _UNITS.items() if k not in ("không", "năm")}
_LAST_UNITS.update({"mốt": 1, "tư": 4, "lăm": 5})
_ZERO_TENS = ("linh", "lẻ")
_SCALES = {"nghìn": 10**3, "ngàn": 10**3, "vạn": 10**4, "triệu": 10**6, "tỷ": 10**9, "tỉ": 10**9}
_WORDS = {
    *_UNITS,
    *_LAST_UNITS,
    *_ZERO_TENS,
    *_SCALES,
    "mười",
    "mươi",
    "đôi",
    "chục",
    "trăm",
    "phẩy",
    "rưỡi",
}
# Longest run of tokens tried as one number.
_MAX_NUMBER_TOKENS = 16

Token = Decimal | str


def _digits_value(text: str) -> Decimal:
    """The value of digits as written: groups of three after one repeated separator are
    thousands ("1.500"), a single other separator is the decimal point ("4,6")."""
    if _GROUPED.fullmatch(text):
        return Decimal(re.sub(r"[.,]", "", text))
    if len(re.findall(r"[.,]", text)) == 1:
        return Decimal(text.replace(",", "."))
    return Decimal(re.sub(r"[.,]", "", text))


def _digits_readings(text: str) -> set[Decimal]:
    """Every value digits in a line may stand for: "1.000" is a thousand in Vietnamese
    and one in English."""
    readings = {_digits_value(text), Decimal(re.sub(r"[.,]", "", text))}
    if len(re.findall(r"[.,]", text)) == 1:
        readings.add(Decimal(text.replace(",", ".")))
    return readings


def source_numbers(text: str) -> list[str]:
    """The numbers of `text` a line has to carry, once each, as the text writes them."""
    text = unicodedata.normalize("NFKC", text)
    found = [m.group(1) + (m.group(2) or "") for m in _SOURCE_NUMBER.finditer(text)]
    return list(dict.fromkeys(found))


def _accepted(number: str) -> set[Decimal]:
    m = _SOURCE_NUMBER.fullmatch(unicodedata.normalize("NFKC", number))
    if m is None:
        raise ValueError(f"not a number: {number!r}")
    value = _digits_value(m.group(1))
    if m.group(2):
        return {value, value * _SOURCE_SCALES[m.group(2)]}
    return {value}


def _digit(token: Token) -> int | None:
    if isinstance(token, Decimal):
        return int(token) if token == token.to_integral_value() and 0 <= token <= 9 else None
    return _UNITS.get(token)


def _tens(ws: list[Token]) -> int | None:
    """0 to 99 in words: "ba", "mười", "mười lăm", "hai mươi", "hai mươi mốt", and the
    idioms "đôi mươi" (20) and "hai chục" (20)."""
    if ws == ["đôi", "mươi"]:
        return 20
    if len(ws) == 2 and ws[1] == "chục" and isinstance(ws[0], str):
        tens = _UNITS.get(ws[0])
        return tens * 10 if tens else None
    if len(ws) == 1:
        return 10 if ws[0] == "mười" else _UNITS.get(ws[0]) if isinstance(ws[0], str) else None
    if ws[0] == "mười":
        last = ws[1] if len(ws) == 2 and isinstance(ws[1], str) else None
        return 10 + _LAST_UNITS[last] if last in _LAST_UNITS else None
    if len(ws) in (2, 3) and ws[1] == "mươi" and isinstance(ws[0], str):
        tens = _UNITS.get(ws[0])
        if tens is None or tens < 2:
            return None
        if len(ws) == 2:
            return tens * 10
        last = ws[2] if isinstance(ws[2], str) else None
        return tens * 10 + _LAST_UNITS[last] if last in _LAST_UNITS else None
    return None


def _hundreds(ws: list[Token]) -> int | None:
    """0 to 999 in words: "một trăm", "một trăm linh năm", "hai trăm mười"."""
    if "trăm" not in ws:
        return _tens(ws)
    if len(ws) < 2 or ws[1] != "trăm" or (hundreds := _digit(ws[0])) is None:
        return None
    rest = ws[2:]
    if not rest:
        return hundreds * 100
    if rest[0] in _ZERO_TENS:
        last = rest[1] if len(rest) == 2 else None
        unit = _digit(last) if isinstance(last, str) else None
        return hundreds * 100 + unit if unit else None
    tens = _tens(rest)
    return hundreds * 100 + tens if tens is not None and tens >= 10 else None


def _integer(ws: list[Token]) -> tuple[Decimal, list[Decimal]] | None:
    """A whole number with its scale words, and the count before each scale word
    ("ba mươi" of "ba mươi nghìn")."""
    if not ws:
        return None
    if len(ws) == 1 and isinstance(ws[0], Decimal):
        return ws[0], []
    scales = [_SCALES[w] for w in ws if isinstance(w, str) and w in _SCALES]
    if not scales:
        value = _hundreds(ws)
        return (Decimal(value), []) if value is not None else None
    scale = max(scales)
    at = next(i for i, w in enumerate(ws) if isinstance(w, str) and _SCALES.get(w) == scale)
    left = _integer(ws[:at])
    if left is None or left[0] <= 0:
        return None
    count, counts = left
    rest = ws[at + 1 :]
    below = Decimal(0)
    if rest:
        if rest[0] in _ZERO_TENS:
            tail = _hundreds(rest[1:])
            if tail is None or not 0 < tail < 100:
                return None
            below = Decimal(tail)
        else:
            right = _integer(rest)
            if right is None or right[0] >= scale:
                return None
            below = right[0]
            counts = counts + right[1]
    return count * scale + below, [count, *counts]


def _fraction(ws: list[Token]) -> str | None:
    """The digits after "phẩy": read one by one ("một bốn") or as a number ("mười bốn")."""
    if not ws:
        return None
    digits = [_digit(w) for w in ws]
    if all(d is not None for d in digits):
        return "".join(str(d) for d in digits)
    whole = _integer(ws)
    if whole is None or whole[1] or whole[0] != whole[0].to_integral_value():
        return None
    return str(int(whole[0]))


def _number(ws: list[Token]) -> list[Decimal] | None:
    """The values `ws` reads as one number: the number itself, then the count before each
    scale word. None if `ws` is not one well-formed number."""
    if "phẩy" in ws:
        at = ws.index("phẩy")
        whole = _integer(ws[:at])
        fraction = _fraction(ws[at + 1 :])
        if whole is None or fraction is None or whole[1]:
            return None
        if whole[0] != whole[0].to_integral_value():
            return None
        return [Decimal(f"{int(whole[0])}.{fraction}")]
    if ws and ws[-1] == "rưỡi":
        if len(ws) < 2:
            return None
        before = ws[-2]
        half = _SCALES.get(before, 100 if before == "trăm" else 1) if isinstance(before, str) else 1
        whole = _integer(ws[:-1])
        if whole is None:
            return None
        return [whole[0] + Decimal(half) / 2, *whole[1]]
    whole = _integer(ws)
    return [whole[0], *whole[1]] if whole is not None else None


def _line_values(line: str) -> set[Decimal]:
    """Every number value `line` writes, in digits or in words."""
    text = unicodedata.normalize("NFC", unicodedata.normalize("NFKC", line)).casefold()
    values: set[Decimal] = set()
    tokens: list[Token] = []
    runs: list[list[Token]] = []
    for raw in _LINE_TOKEN.findall(text):
        if raw[0].isdigit():
            values |= _digits_readings(raw)
            tokens.append(_digits_value(raw))
        elif raw in _WORDS:
            tokens.append(raw)
        else:
            runs.append(tokens)
            tokens = []
    runs.append(tokens)
    for run in runs:
        start = 0
        while start < len(run):
            for end in range(min(len(run), start + _MAX_NUMBER_TOKENS), start, -1):
                read = _number(run[start:end])
                if read is not None:
                    values.update(read)
                    start = end
                    break
            else:
                start += 1
    return values


def carries_number(number: str, line: str) -> bool:
    """Whether `line` carries `number`, one of `source_numbers` of its source."""
    return not _accepted(number).isdisjoint(_line_values(line))


def missing_numbers(source: str, line: str) -> list[str]:
    """The numbers of `source` that `line` does not carry, as the source writes them."""
    values = _line_values(line)
    return [n for n in source_numbers(source) if _accepted(n).isdisjoint(values)]

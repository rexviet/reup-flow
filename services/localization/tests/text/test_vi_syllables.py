"""Counting syllables of a Vietnamese line, with numbers spoken and Latin words
estimated. Every expected count below is worked out by hand from how the line is
read aloud; none is copied from the function's own output."""

import pytest

from localization.text.vi_syllables import (
    count_syllables,
    estimate_ms,
    numbers_to_words,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hôm nay trời đẹp quá", 5),
        ("Xin chào các bạn", 4),
        ("", 0),
        ("...", 0),
        # "hai nghìn lẻ hai mươi bốn" is six syllables where the digits are one token.
        ("năm 2024 mua iPhone", 1 + 6 + 1 + 2),
        # giảm | năm mươi phần trăm | còn | một triệu năm trăm nghìn | đồng
        ("giảm 50% còn 1.500.000 đồng", 1 + 4 + 1 + 5 + 1),
        # giá | ba phẩy năm | triệu
        ("giá 3,5 triệu", 1 + 3 + 1),
        # mua | năm mươi nghìn
        ("mua 50k", 1 + 3),
        # một trăm đô la
        ("$100", 4),
        # TikTok: tích tóc
        ("TikTok là AI", 2 + 1 + 2),
        # Apple (ap-ple) Store (one syllable in English) ở đây
        ("Apple Store ở đây", 2 + 1 + 1 + 1),
    ],
)
def test_syllables_of_a_line_as_it_is_read_aloud(text: str, expected: int) -> None:
    assert count_syllables(text) == expected


def test_a_year_is_not_one_syllable() -> None:
    """Without turning digits into words the line below would be counted as 4."""
    assert count_syllables("năm 2024 mua iPhone") > 4 + 3


def test_numbers_become_the_words_that_read_them() -> None:
    assert numbers_to_words("15").strip() == "mười lăm"
    assert numbers_to_words("2024").strip() == "hai nghìn lẻ hai mươi bốn"
    assert " ".join(numbers_to_words("50%").split()) == "năm mươi phần trăm"
    assert " ".join(numbers_to_words("3,5").split()) == "ba phẩy năm"


def test_a_decimal_is_read_digit_by_digit_after_the_comma() -> None:
    """The library reads the fraction of 3.5 as 'năm mươi'; speech says 'năm'."""
    assert " ".join(numbers_to_words("3.25").split()) == "ba phẩy hai năm"


def test_a_thousands_separator_is_not_a_decimal_point() -> None:
    assert " ".join(numbers_to_words("1.000").split()) == "một nghìn"
    assert " ".join(numbers_to_words("1,000,000").split()) == "một triệu"


def test_a_latin_word_is_counted_by_its_vowel_groups() -> None:
    assert count_syllables("iPhone") == 2  # the final e is silent
    assert count_syllables("Google") == 2  # but the e after l is a syllable
    assert count_syllables("Apple") == 2


def test_a_short_capital_word_is_spelled_letter_by_letter() -> None:
    assert count_syllables("USB") == 3
    assert count_syllables("VIP") == 3


def test_toneless_vietnamese_words_are_one_syllable_each() -> None:
    assert count_syllables("ba nha mua") == 3


def test_chinese_characters_left_in_a_line_count_one_each() -> None:
    assert count_syllables("xem 你好 nhé") == 1 + 2 + 1


def test_a_huge_number_does_not_crash_the_count() -> None:
    assert count_syllables("9" * 40) > 10


def test_estimate_is_syllables_over_rate() -> None:
    assert estimate_ms("hôm nay trời đẹp quá", 5.0) == 1000
    assert estimate_ms("hôm nay trời đẹp quá", 4.0) == 1250


def test_a_non_positive_rate_is_refused() -> None:
    with pytest.raises(ValueError):
        estimate_ms("xin chào", 0)

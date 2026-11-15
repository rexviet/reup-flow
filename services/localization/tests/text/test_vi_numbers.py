"""Whether a Vietnamese line carries a number of its source, in digits or in words. Every
expected verdict is worked out by hand from how the number is read in Vietnamese."""

import pytest
from num2words import num2words

from localization.text.vi_numbers import carries_number, missing_numbers, source_numbers


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30岁更像20岁", ["30", "20"]),
        ("4.6安纳铁", ["4.6"]),
        ("每100个女生70个", ["100", "70"]),
        ("共1,500元", ["1,500"]),
        ("50%的人", ["50"]),
        ("3万块", ["3万"]),
        ("iPhone15和15寸", ["15"]),
        ("没有数字", []),
    ],
)
def test_the_numbers_of_a_source_are_read_whole(text: str, expected: list[str]) -> None:
    assert source_numbers(text) == expected


@pytest.mark.parametrize(
    ("number", "line"),
    [
        ("30", "Ba mươi tuổi mà trông như hai mươi"),
        ("20", "Ba mươi tuổi mà trông như hai mươi"),
        ("4.6", "bốn phẩy sáu miligam"),
        ("4.6", "4,6 mg"),
        ("4.6", "4.6 mg"),
        ("100", "cứ một trăm bạn nữ thì bảy mươi bạn"),
        ("70", "cứ một trăm bạn nữ thì bảy mươi bạn"),
        ("21", "hai mươi mốt ngày"),
        ("24", "hai mươi tư giờ"),
        ("25", "hai mươi lăm"),
        ("15", "mười lăm phút"),
        ("11", "mười một"),
        ("10", "mười năm"),
        ("105", "một trăm linh năm"),
        ("105", "một trăm lẻ năm"),
        ("150", "một trăm năm mươi"),
        ("150", "một trăm rưỡi"),
        ("1000", "một nghìn"),
        ("1000", "một ngàn"),
        ("1000", "1.000 đồng"),
        ("1,500", "1.500 đồng"),
        ("1500", "một nghìn năm trăm"),
        ("1500", "một nghìn rưỡi"),
        ("2024", "hai nghìn không trăm hai mươi bốn"),
        ("2024", "hai nghìn lẻ hai mươi bốn"),
        ("1200000", "một triệu hai trăm nghìn"),
        ("2.5", "hai rưỡi"),
        ("0.5", "không phẩy năm"),
        ("3.14", "ba phẩy mười bốn"),
        ("3.14", "ba phẩy một bốn"),
        ("50", "năm mươi phần trăm"),
        ("50", "50%"),
        # A number given as a count of thousands keeps its count: 30 yuan, said as 30
        # thousand dong.
        ("30", "chỉ ba mươi nghìn"),
        ("30", "chỉ 30 nghìn"),
        ("3万", "ba mươi nghìn"),
        ("3万", "3 vạn"),
        ("07", "số 7"),
        ("15", "iPhone15"),
    ],
)
def test_a_number_written_in_digits_or_in_words_is_carried(number: str, line: str) -> None:
    assert carries_number(number, line)


@pytest.mark.parametrize(
    ("number", "line"),
    [
        # "đôi mươi" is twenty, said of an age.
        ("20", "Ba mươi tuổi mà trông như đôi mươi"),
        ("20", "tuổi Đôi Mươi"),
        # A digit word before "chục" counts its tens.
        ("10", "một chục quả trứng"),
        ("20", "hai chục người"),
        ("50", "năm chục"),
        ("90", "chín chục"),
        ("30", "ba chục nghìn"),
        ("30000", "ba chục nghìn đồng"),
        ("120", "một trăm hai chục"),
    ],
)
def test_an_idiom_that_names_one_number_is_carried(number: str, line: str) -> None:
    assert carries_number(number, line)


@pytest.mark.parametrize(
    ("number", "line"),
    [
        # A pair, or a vague count of tens, names no number.
        ("2", "một đôi giày"),
        ("2", "đôi khi"),
        ("20", "đôi"),
        ("21", "đôi mươi mốt"),
        ("10", "chục người"),
        ("10", "vài chục người"),
        ("20", "vài chục người"),
        ("10", "mấy chục năm"),
        ("10", "hàng chục lần"),
        ("10", "dăm chục"),
        ("100", "mười chục"),
        ("2", "đôi mươi"),
        # "tá" is also a syllable of words ("đại tá", "tá điền", "phụ tá"), and "một tá"
        # is often only "a lot".
        ("12", "một tá"),
        ("24", "hai tá"),
        # "Ngũ" is a Sino-Vietnamese syllable of a name, not the number five.
        ("5", "Ngũ"),
        ("5", "Anh Ngũ"),
    ],
)
def test_a_pair_a_vague_count_or_a_name_syllable_is_not_a_number(number: str, line: str) -> None:
    assert not carries_number(number, line)


@pytest.mark.parametrize(
    ("number", "line"),
    [
        ("70", "cứ một trăm bạn nữ thì có bạn"),
        ("70", "bảy mươi lăm bạn"),
        ("75", "bảy mươi năm"),
        ("20", "một trăm hai mươi"),
        ("2", "20 người"),
        ("4.6", "bốn sáu"),
        ("4.6", "4 và 6"),
        ("6", "bốn phẩy sáu"),
        ("105", "một trăm năm"),
        ("15", "mười năm"),
        ("30", "ba"),
        ("1000", "nghìn người"),
    ],
)
def test_a_dropped_or_wrong_number_is_not_carried(number: str, line: str) -> None:
    assert not carries_number(number, line)


@pytest.mark.parametrize("n", [*range(0, 1100), 1999, 2024, 10_000, 99_999, 1_200_000, 10**9])
def test_every_number_read_by_num2words_is_carried(n: int) -> None:
    assert carries_number(str(n), num2words(n, lang="vi"))


def test_the_missing_numbers_are_named_as_the_source_wrote_them() -> None:
    assert missing_numbers("30岁更像20岁", "Ba mươi tuổi mà trông như hai mươi") == []
    assert missing_numbers("4.6安纳铁", "bốn phẩy sáu miligam") == []
    assert missing_numbers("每100个女生70个", "cứ một trăm bạn nữ thì có bạn") == ["70"]
    assert missing_numbers("4.6安纳铁", "sắt") == ["4.6"]

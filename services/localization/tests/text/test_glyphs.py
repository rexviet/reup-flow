"""Characters a Vietnamese caption cannot show, told apart without the font file."""

import pytest

from localization.text.glyphs import foreign_chars


@pytest.mark.parametrize(
    "text",
    [
        "Xin chào các bạn, hôm nay mình sẽ chia sẻ",
        "Đường Nguyễn Huệ, quận 1: giá 1.500.000₫ (giảm 30%)",
        "Một + hai = ba; 5 × 4 ÷ 2 ± 1",
        'iPhone 15 Pro — “tuyệt vời”… ‘ok’ - "hay"',
        "Ǻ ǵ ẞ ȃ ǆ",  # more Latin letters than Vietnamese needs
        "Ngày 3/4/2024 lúc 9:30, email: a@b",
        "tên: Nguyễn (NFD) = Nguyễn",
        "",
        "\t\n  ",
    ],
)
def test_text_a_vietnamese_caption_can_hold_is_not_flagged(text: str) -> None:
    assert foreign_chars(text) == []


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("你好 bạn", ["你", "好"]),
        ("Xin chào 😀", ["😀"]),
        ("ok → tiếp", ["→"]),
        ("đúng ✓", ["✓"]),
        ("vâng，rồi！", ["，", "！"]),
        ("trời。", ["。"]),
        ("tên: 「Lan」", ["「", "」"]),
        ("привет", list("привет")),
        ("가나다", ["가", "나", "다"]),
        ("ひらがな", list("ひらがな")),
        ("a ∑ b", ["∑"]),
        ("a ♥ b", ["♥"]),
    ],
)
def test_text_the_font_cannot_draw_is_flagged_in_order_without_repeats(
    text: str, flagged: list[str]
) -> None:
    assert foreign_chars(text) == list(dict.fromkeys(flagged))


def test_a_repeated_character_is_named_once() -> None:
    assert foreign_chars("你你你好你") == ["你", "好"]

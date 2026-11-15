import pytest

from localization.text.repeats import collapse_repeats


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("啊" * 60, "啊"),
        ("啊啊啊！救命", "啊！救命"),
        ("ㅋㅋㅋㅋ 진짜", "ㅋ 진짜"),
        ("Aaaah!", "Ah!"),
        ("á á á á á", "á"),
        ("no, no, no! Stop.", "no! Stop."),
        ("Ha ha ha ha, funny", "Ha, funny"),
        ("啊 啊啊啊 啊", "啊"),  # the letter run first, then the word run it leaves
    ],
)
def test_a_letter_or_word_said_three_or_more_times_is_kept_once(source: str, expected: str) -> None:
    assert collapse_repeats(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "I'm top of the game.",
        "창재한! 너 도와주는 거 말이야!",
        "啊啊 好",  # twice is not a run
        "no no, stop",
        "1000 người, 333 đồng",  # digits are numbers
        "",
    ],
)
def test_text_without_a_run_of_three_is_unchanged(source: str) -> None:
    assert collapse_repeats(source) == source

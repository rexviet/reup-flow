"""Which words a voice dropped, judged from what was heard."""

from localization.text.word_coverage import lost_too_many, lost_words

LINE = "Mặt thì xinh xắn, dáng thanh mảnh, sao nhảy khỏe thế?"


def test_a_dropped_clause_is_named():
    assert lost_words(LINE, "Mặt thì xinh xắn sao nhảy khỏe thế") == ["dang", "thanh", "manh"]


def test_a_dropped_clause_makes_the_clip_wrong():
    assert lost_too_many(LINE, "Mặt thì xinh xắn sao nhảy khỏe thế")


def test_a_whole_line_is_whole_whatever_the_tone_marks_or_case():
    assert lost_too_many(LINE, "mặt thì xinh xắn, dáng thanh mảnh, sao nhảy khoẻ thế") == []
    assert lost_too_many(LINE, "MAT THI XINH XAN DANG THANH MANH SAO NHAY KHOE THE") == []


def test_one_missing_word_is_a_transcription_error_not_a_lost_clause():
    long_line = "Hôm nay mình sẽ chia sẻ cách chọn mua điện thoại mới trong năm nay"
    heard = "Hôm nay mình sẽ chia sẻ cách chọn mua điện thoại mới trong năm"
    assert lost_too_many(long_line, heard) == []


def test_a_number_is_compared_as_the_words_that_read_it():
    assert lost_too_many("Giá giảm 30% hôm nay", "Giá giảm ba mươi phần trăm hôm nay") == []


def test_a_word_the_line_repeats_is_lost_once_when_said_once():
    assert lost_words("đi đi đi về", "đi đi về") == ["di"]


def test_nothing_heard_loses_everything():
    assert len(lost_words(LINE, "")) == 11

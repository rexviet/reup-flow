"""The slot a line is offered: the segment's span, extended through the silence
after it, with a cap and a floor."""

from localization.text.slots import MAX_EXTENSION_MS, MIN_SLOT_MS, slots_ms


def test_a_segment_borrows_the_silence_up_to_the_next_one() -> None:
    # 0-1000 then 1800-2800: the 800 ms of quiet is part of the first slot.
    assert slots_ms([(0, 1000), (1800, 2800)], duration_ms=3000) == [1800, 1000 + 200]


def test_the_extension_is_capped_so_one_long_silence_is_not_given_to_one_line() -> None:
    slots = slots_ms([(0, 2000), (20_000, 22_000)], duration_ms=40_000)
    assert slots == [2000 + MAX_EXTENSION_MS, 2000 + MAX_EXTENSION_MS]


def test_the_last_segment_extends_towards_the_end_of_the_video_and_no_further() -> None:
    assert slots_ms([(0, 2000)], duration_ms=2500) == [2500]
    assert slots_ms([(0, 2000)], duration_ms=10_000) == [2000 + MAX_EXTENSION_MS]


def test_without_a_video_length_the_last_segment_gets_the_capped_extension() -> None:
    assert slots_ms([(0, 2000)], duration_ms=None) == [2000 + MAX_EXTENSION_MS]


def test_a_segment_collapsed_to_a_few_milliseconds_still_gets_a_usable_slot() -> None:
    slots = slots_ms([(3464, 3504), (3510, 6000)], duration_ms=7000)
    assert slots[0] == MIN_SLOT_MS


def test_a_segment_that_overlaps_the_next_is_not_extended_backwards() -> None:
    # The next one starts before this one ends: the slot is at least its own span.
    assert slots_ms([(0, 2000), (1500, 3000)], duration_ms=4000)[0] == 2000


def test_no_segments_no_slots() -> None:
    assert slots_ms([], duration_ms=1000) == []

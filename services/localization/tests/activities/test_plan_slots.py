"""Where each line of the Vbee dub is spoken: speech found in the vocals, merged into
slots cut only at pauses, and the transcript's text shared over the slots by code,
every cut at a phrase end."""

import re
import subprocess
import tempfile
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from temporalio.testing import ActivityEnvironment

from localization.activities.plan_slots import (
    MAX_SHIFT_WORDS,
    MIN_SPEECH_MS,
    SLOT_MAX_MS,
    SLOT_MIN_MS,
    PlanSlotsActivity,
    Span,
    detect_speech,
    group_slots,
    parse_silences,
    plan_slots,
    speech_chunks,
)
from localization.contracts.vbee_flow import SlotPlan, SpeechTranscript, Utterance
from localization.storage.object_store import ArtifactMissingError, ObjectStore, work_key
from tests.activities.plan_slots_real import (
    AUDIO_MS_7616,
    SILENCES_7616,
    UTTERANCES_7616,
)
from tests.support.inputs import make_input

SPEC = make_input()
RATE = 16000
# silencedetect reports to the sample; a generated edge lands within a few ms.
EDGE_MS = 30


def wav(path: Path, seconds: float, speech: list[tuple[float, float]], *, noise=False) -> Path:
    """A WAV that is loud inside each (start, end) span and silent outside it."""
    inside = "+".join(f"between(t,{a},{b})" for a, b in speech) or "0"
    sound = "0.5*(2*random(0)-1)" if noise else "0.5*sin(2*PI*440*t)"
    expr = f"if({inside},{sound},0)"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi"]
        + ["-i", f"aevalsrc='{expr}':s={RATE}:d={seconds}", str(path)],
        check=True,
    )
    return path


def transcript(*utterances: tuple[str, int, int], audio_ms: int = 60_000) -> SpeechTranscript:
    return SpeechTranscript(
        utterances=tuple(Utterance(text=t, start_ms=a, end_ms=b) for t, a, b in utterances),
        audio_ms=audio_ms,
    )


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def assert_text_kept(plan: SlotPlan, source: SpeechTranscript) -> None:
    said = "".join(squash(s.text) for s in plan.slots)
    assert said == "".join(squash(u.text) for u in source.utterances)


def regular_speech(total_ms: int, talk_ms: int, pause_ms: int) -> list[Span]:
    chunks, t = [], 0
    while t + talk_ms <= total_ms:
        chunks.append(Span(t, t + talk_ms))
        t += talk_ms + pause_ms
    return chunks


def real_7616() -> tuple[list[Span], SpeechTranscript]:
    silences = [Span(a, b) for a, b in SILENCES_7616]
    source = SpeechTranscript(
        utterances=tuple(Utterance(text=t, start_ms=a, end_ms=b) for t, a, b in UTTERANCES_7616),
        audio_ms=AUDIO_MS_7616,
    )
    return speech_chunks(silences, AUDIO_MS_7616), source


# --- finding speech in the vocals ------------------------------------------------------


@pytest.mark.parametrize("noise", [False, True])
def test_speech_is_found_between_silences_of_known_length(tmp_path, noise) -> None:
    speech = [(0.5, 2.0), (2.4, 4.0), (5.0, 7.5)]
    chunks, audio_ms = detect_speech(wav(tmp_path / "v.wav", 9.0, speech, noise=noise))
    assert abs(audio_ms - 9000) <= EDGE_MS
    assert len(chunks) == len(speech)
    for got, (a, b) in zip(chunks, speech, strict=True):
        assert abs(got.start_ms - a * 1000) <= EDGE_MS
        assert abs(got.end_ms - b * 1000) <= EDGE_MS


def test_a_pause_shorter_than_the_minimum_does_not_split_speech(tmp_path) -> None:
    # 0.1 s is a consonant's closure, not a pause between phrases.
    chunks, _ = detect_speech(wav(tmp_path / "v.wav", 4.0, [(0.0, 1.5), (1.6, 3.0)]))
    assert len(chunks) == 1


def test_speech_running_to_both_ends_is_one_chunk(tmp_path) -> None:
    chunks, audio_ms = detect_speech(wav(tmp_path / "v.wav", 3.0, [(0.0, 3.0)]))
    assert chunks == [Span(0, audio_ms)]


def test_a_silence_still_open_at_the_end_runs_to_the_end() -> None:
    log = "silence_start: -0.002\nsilence_end: 1.5 | silence_duration: 1.5\nsilence_start: 3.25\n"
    assert parse_silences(log, 4000) == [Span(0, 1500), Span(3250, 4000)]


def test_speech_chunks_are_what_the_silences_leave() -> None:
    assert speech_chunks([Span(0, 500), Span(2000, 2500)], 4000) == [
        Span(500, 2000),
        Span(2500, 4000),
    ]
    assert speech_chunks([], 4000) == [Span(0, 4000)]
    assert speech_chunks([Span(0, 4000)], 4000) == []


# --- no speech -------------------------------------------------------------------------


def test_a_silent_track_has_no_speech_and_no_slots(tmp_path) -> None:
    chunks, audio_ms = detect_speech(wav(tmp_path / "v.wav", 5.0, []))
    plan = plan_slots(chunks, audio_ms, transcript(("Xin chào.", 0, 2000), audio_ms=audio_ms))
    assert chunks == []
    assert plan.has_speech is False and plan.slots == ()


def test_a_couple_of_words_in_a_music_track_are_not_speech(tmp_path) -> None:
    # Two isolated words, as on a music track where only "thank / you" crosses -30 dB.
    path = wav(tmp_path / "v.wav", 20.0, [(16.2, 16.9), (19.8, 20.0)])
    chunks, audio_ms = detect_speech(path)
    assert 0 < sum(c.end_ms - c.start_ms for c in chunks) < MIN_SPEECH_MS
    plan = plan_slots(chunks, audio_ms, transcript(("Cảm ơn.", 16_000, 20_000)))
    assert plan.has_speech is False and plan.slots == ()


def test_speech_without_a_transcript_has_nothing_to_dub() -> None:
    plan = plan_slots([Span(0, 8000)], 8000, transcript(audio_ms=8000))
    assert plan.has_speech is False and plan.slots == ()
    assert plan.notes


# --- slots -----------------------------------------------------------------------------


def test_slots_are_three_to_ten_seconds_and_cut_only_at_pauses() -> None:
    chunks = regular_speech(90_000, 1200, 300)
    for start, end in group_slots(chunks, 90_000):
        assert SLOT_MIN_MS <= end - start <= SLOT_MAX_MS
        assert start in {c.start_ms for c in chunks}
        # The slot ends in the pause after its last chunk, never inside a chunk.
        assert not any(c.start_ms < end < c.end_ms for c in chunks)


def test_a_slot_is_cut_at_the_long_pause_rather_than_a_short_one() -> None:
    # Phrases of 1.5 s with 0.2 s breaths, and one 0.8 s pause at about 6 s.
    chunks = [Span(0, 1500), Span(1700, 3200), Span(3400, 4900), Span(5100, 6100)]
    chunks += [Span(6900, 8400), Span(8600, 10_100), Span(10_300, 11_800)]
    slots = group_slots(chunks, 12_500)
    assert [s.start_ms for s in slots] == [0, 6900]


def test_a_long_silence_is_not_given_to_the_slot_before_it() -> None:
    # 5 s with nobody speaking: the line before it has a second of slack, not five.
    chunks = [Span(0, 4000), Span(9000, 13_000)]
    first, second = group_slots(chunks, 14_000)
    assert first == Span(0, 5000) and second.start_ms == 9000


def test_speech_without_any_pause_is_one_slot() -> None:
    assert group_slots([Span(1000, 25_000)], 26_000) == [Span(1000, 26_000)]


def test_real_7616_slots_are_within_bounds_and_end_in_pauses() -> None:
    chunks, source = real_7616()
    plan = plan_slots(chunks, source.audio_ms, source)
    assert plan.has_speech
    for slot in plan.slots:
        assert slot.slot_ms <= SLOT_MAX_MS
        assert not any(c.start_ms < slot.end_ms < c.end_ms for c in chunks)
        assert not any(c.start_ms < slot.start_ms < c.end_ms for c in chunks)
    assert all(s.slot_ms >= SLOT_MIN_MS for s in plan.slots[:-1])


# --- text over the slots ---------------------------------------------------------------


def test_real_7616_keeps_every_word_once_in_order() -> None:
    chunks, source = real_7616()
    assert_text_kept(plan_slots(chunks, source.audio_ms, source), source)


def test_real_7616_every_slot_ends_at_a_phrase_end() -> None:
    chunks, source = real_7616()
    plan = plan_slots(chunks, source.audio_ms, source)
    assert len(plan.slots) >= 8
    for slot in plan.slots:
        assert slot.text.rstrip()[-1] in ".,!?;:…", slot.text


def test_real_7616_never_cuts_inside_a_number_or_before_its_unit() -> None:
    chunks, source = real_7616()
    plan = plan_slots(chunks, source.audio_ms, source)
    holding = [s.text for s in plan.slots if "4,6" in s.text]
    assert len(holding) == 1 and "4,6 mg" in holding[0]
    assert any("146 loại" in s.text for s in plan.slots)


def test_real_7616_overlapping_utterances_do_not_interleave() -> None:
    # Vbee's first utterance ends at 23.06 s, the second starts at 22.38 s; the pause
    # inside the overlap (22.39 to 22.88 s) is where one hands over to the other.
    chunks, source = real_7616()
    plan = plan_slots(chunks, source.audio_ms, source)
    first_end = next(i for i, s in enumerate(plan.slots) if "Five Girls Doctor." in s.text)
    second = next(i for i, s in enumerate(plan.slots) if "Nó khác với" in s.text)
    assert plan.slots[first_end].text.endswith("Doctor.")
    assert second == first_end + 1
    assert plan.slots[second].start_ms == 22_876


def test_overlap_without_a_pause_is_shared_at_its_middle() -> None:
    chunks = [Span(0, 20_000)]
    source = transcript(
        ("Một hai ba bốn năm, sáu bảy tám chín mười.", 0, 10_400),
        ("Mười một mười hai, mười ba mười bốn.", 9600, 20_000),
    )
    plan = plan_slots(chunks, 20_000, source)
    assert_text_kept(plan, source)


def test_a_cut_moves_to_the_nearest_phrase_end() -> None:
    # Even speech over 12 s: time alone would cut after "bảy" (word 7 of 14), but the
    # nearest phrase end is after "bốn," (word 4).
    chunks = [Span(0, 5800), Span(6200, 12_000)]
    text = "Một hai ba bốn, năm sáu bảy tám chín mười mười một mười hai."
    plan = plan_slots(chunks, 12_000, transcript((text, 0, 12_000)))
    assert [s.text for s in plan.slots] == [
        "Một hai ba bốn,",
        "năm sáu bảy tám chín mười mười một mười hai.",
    ]


def test_without_a_phrase_end_near_the_cut_the_slots_are_one() -> None:
    chunks = [Span(0, 5800), Span(6200, 12_000)]
    words = [f"từ{i}" for i in range(4 * MAX_SHIFT_WORDS)]
    text = "Đầu, " + " ".join(words) + "."
    plan = plan_slots(chunks, 12_000, transcript((text, 0, 12_000)))
    assert len(plan.slots) == 1 and plan.slots[0].text == text
    assert any("phrase end" in note for note in plan.notes)


def test_a_number_and_its_unit_stay_together_without_punctuation() -> None:
    # No punctuation at all, so any word boundary may be cut; time alone would cut
    # between "4,6" and "mg".
    chunks = [Span(0, 4000), Span(4300, 8000)]
    text = "mỗi viên nhỏ có 4,6 mg sắt cho một ngày"
    plan = plan_slots(chunks, 8000, transcript((text, 0, 8000)))
    assert_text_kept(plan, transcript((text, 0, 8000)))
    assert any("4,6 mg" in s.text for s in plan.slots)


def test_an_utterance_without_punctuation_is_cut_at_word_boundaries() -> None:
    chunks = [Span(0, 4000), Span(4300, 8300)]
    text = "một hai ba bốn năm sáu bảy tám chín mười"
    plan = plan_slots(chunks, 8300, transcript((text, 0, 8300)))
    assert [s.text for s in plan.slots] == ["một hai ba bốn năm", "sáu bảy tám chín mười"]


def test_a_full_stop_only_at_the_end_still_allows_word_boundaries() -> None:
    chunks = [Span(0, 4000), Span(4300, 8300)]
    text = "một hai ba bốn năm sáu bảy tám chín mười."
    plan = plan_slots(chunks, 8300, transcript((text, 0, 8300)))
    assert [s.text for s in plan.slots] == ["một hai ba bốn năm", "sáu bảy tám chín mười."]


def test_text_is_shared_by_syllables_so_a_number_counts_as_read_aloud() -> None:
    # "100%" is read "một trăm phần trăm": four syllables, not one word's worth.
    chunks = [Span(0, 4000), Span(4300, 8300)]
    text = "giảm 100% mỡ thừa cho người béo phì lâu năm"
    plan = plan_slots(chunks, 8300, transcript((text, 0, 8300)))
    assert [s.text for s in plan.slots] == ["giảm 100% mỡ thừa", "cho người béo phì lâu năm"]


def test_an_overlap_is_handed_over_at_the_pause_inside_it() -> None:
    # Vbee's spans overlap from 3 to 5 s; the speaker pauses at 3.6 to 3.8 s. The
    # middle of the overlap (4 s) would give the first utterance 0.2 s of the second
    # one's speech, and its last word to the second slot.
    chunks = [Span(0, 3600), Span(3800, 9000)]
    first = " ".join(["một"] * 20)
    source = transcript((first, 0, 5000), ("Năm sáu, bảy tám.", 3000, 9000))
    plan = plan_slots(chunks, 10_000, source)
    assert [s.text for s in plan.slots] == [first, "Năm sáu, bảy tám."]


def test_unbroken_runs_of_speech_longer_than_a_minute_are_a_slot_each() -> None:
    chunks = [Span(0, 70_000), Span(70_500, 140_000)]
    assert group_slots(chunks, 140_000) == [Span(0, 70_500), Span(70_500, 140_000)]


def test_an_utterance_boundary_is_a_cut_even_without_punctuation() -> None:
    chunks = [Span(0, 4000), Span(4300, 8300)]
    # Each has a comma, so only phrase ends are cuts; Vbee ends an utterance at a pause.
    source = transcript(("Một hai, ba bốn năm", 0, 4100), ("sáu bảy, tám chín mười", 4200, 8300))
    plan = plan_slots(chunks, 8300, source)
    assert [s.text for s in plan.slots] == ["Một hai, ba bốn năm", "sáu bảy, tám chín mười"]


def test_speech_outside_every_utterance_gets_no_slot() -> None:
    # Vbee heard nothing after 9 s: a cough or a sung note there is not a line.
    chunks = [Span(0, 4000), Span(4300, 8500), Span(12_000, 16_000)]
    source = transcript(("Một hai ba, bốn năm sáu.", 0, 8600))
    plan = plan_slots(chunks, 16_000, source)
    assert plan.slots[-1].end_ms <= 12_000
    assert_text_kept(plan, source)


def test_an_utterance_where_the_vocals_are_silent_joins_the_nearest_slot() -> None:
    chunks = [Span(0, 4000), Span(4300, 8500)]
    source = transcript(("Một hai ba, bốn năm sáu.", 0, 8600), ("Hết.", 9000, 9500))
    plan = plan_slots(chunks, 10_000, source)
    assert plan.slots[-1].text.endswith("Hết.")
    assert_text_kept(plan, source)


def test_slot_ids_are_in_order_and_slots_do_not_overlap() -> None:
    chunks, source = real_7616()
    plan = plan_slots(chunks, source.audio_ms, source)
    # The contract itself refuses anything else; this pins that the plan is built.
    assert [s.id for s in plan.slots] == list(range(len(plan.slots)))
    assert plan.slots[-1].end_ms <= source.audio_ms


# --- the activity ----------------------------------------------------------------------


@pytest.fixture
def store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="reup-media")
        yield ObjectStore(client, "reup-media")


async def test_the_activity_reads_the_vocals_and_plans_slots(store, tmp_path, monkeypatch) -> None:
    speech = [(0.0, 2.8), (3.2, 6.0), (6.6, 9.4), (9.8, 12.0)]
    key = work_key(SPEC.aweme_id, 0, "vocals.wav")
    store.put_file(key, wav(tmp_path / "v.wav", 12.5, speech))
    source = transcript(
        ("Ba mươi tuổi trông như hai mươi tuổi, sức sống hướng thượng.", 0, 6100),
        ("Ăn sạch sẽ, tập thể dục đều đặn, nghỉ ngơi đúng giờ.", 6500, 12_000),
        audio_ms=12_500,
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    plan = await ActivityEnvironment().run(PlanSlotsActivity(store).plan_slots, SPEC, key, source)

    assert plan.has_speech and len(plan.slots) == 2
    assert plan.slots[0].start_ms <= EDGE_MS
    assert plan.slots[1].text.startswith("Ăn sạch sẽ")
    assert_text_kept(plan, source)
    assert list(scratch.iterdir()) == []


async def test_the_activity_fails_for_good_when_the_vocals_are_gone(store) -> None:
    key = work_key(SPEC.aweme_id, 0, "vocals.wav")
    with pytest.raises(ArtifactMissingError):
        await ActivityEnvironment().run(
            PlanSlotsActivity(store).plan_slots, SPEC, key, transcript(("Một.", 0, 1000))
        )

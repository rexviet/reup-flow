"""The Vbee dubbing workflow end to end on a local Temporal server: the order of its steps
and their queues, the rate measured on the spoken clips, the lines a failed review sends
back to the shortener, a video without speech, a failure that is written on the run, and
the configured voice that never reaches the history.

The voice, the synthesizer, its cache, the script and dub documents, the STT step, the
shortener agents and publishing are real (see tests/support/vbee_fakes.py)."""

import pytest
from temporalio.client import Client

from localization.activities.voice_rate import SAMPLE_TEXT
from localization.contracts import DubSegmentsDocument
from localization.providers.errors import PermanentProviderError
from tests.support.review_fakes import ReviewFakes
from tests.support.vbee_fakes import (
    SLOW_WORD,
    FakeVbeeStt,
    FakeVbeeVoice,
    ScriptedShortener,
    VbeeRun,
    run_vbee,
    three_slots,
    words,
)

PREPARE = ["ingest", "detect_subtitles", "voice_rate", "vbee_stt", "plan_slots"]
SHORTEN = "agent__slot_shortener__model_request"
SHORTEN_REPAIR = "agent__slot_shortener_repair__model_request"


def spoken(run: VbeeRun) -> dict[str, int]:
    """How many times the voice was asked for each line (the sample sentence aside)."""
    return {t: n for t, n in run.voice.calls.items() if t != SAMPLE_TEXT}


async def test_a_run_goes_from_the_vocals_to_one_published_render_in_order(
    temporal_client: Client,
) -> None:
    # The middle line has 30 syllables: 5000 ms at 6 a second in a 4000 ms slot, over the
    # 4600 the fastest stretch allows, so it is shortened before anything is spoken.
    run = await run_vbee(
        temporal_client, VbeeRun(slots=three_slots(words(10), words(30), words(10)))
    )
    assert run.result.publish.outcome == "published"
    assert await run.scheduled() == [
        *PREPARE,
        SHORTEN,
        "write_slot_script",
        "plan_synthesis",
        # Three lines in two even batches, one per concurrent `tts` slot.
        "synthesize_segments",
        "synthesize_segments",
        "clip_durations",
        "commit_dub",
        "assemble_audio",
        "layout_captions",
        "review_dub",
        "render",
        "publish",
    ]
    assert await run.queues() == {
        "ingest": "media",
        "detect_subtitles": "io",
        "voice_rate": "tts",
        "vbee_stt": "stt",
        "plan_slots": "io",
        SHORTEN: "llm",
        "write_slot_script": "io",
        "plan_synthesis": "io",
        "synthesize_segments": "tts",
        "clip_durations": "io",
        "commit_dub": "io",
        "assemble_audio": "media",
        "layout_captions": "media",
        "review_dub": "io",
        "render": "render",
        "publish": "io",
    }
    # What Vbee heard is what the slots were planned from, on the separated vocals.
    ((vocals_key, transcript),) = run.planned_from
    assert vocals_key == "work/7001/r0/vocals.wav" and transcript == run.stt.answer
    # Only the line that overran was asked for, at the sample's rate (a 24-syllable budget).
    (prompt,) = run.shortener.prompts
    assert ScriptedShortener.asked(prompt) == {1: 24}
    # The line spoken is the shortened one; each line was spoken once.
    assert spoken(run) == {words(10): 1, words(23, "gọn"): 1}
    assert [s.text for s in run.result.slots.slots] == [words(10), words(23, "gọn"), words(10)]
    assert run.result.slots.shortened_ids == (1,)
    # The script the track, the captions and the review were made from is the first one.
    assert run.result.script.script.script_key == "work/7001/r0/script.v1.json"
    assert run.result.dub.script_key == run.result.script.script.script_key
    assert run.row()["status"] == "published" and run.row()["review_rounds"] == 1
    assert run.reviewing.calls["render"] == 1


async def test_lines_that_overrun_at_the_rate_measured_on_the_clips_are_shortened_once_more(
    temporal_client: Client,
) -> None:
    """The voice reads the sample at 6 syllables a second and real lines at 4.5. A line of
    24 syllables fits its 4000 ms slot at 6 (4000 ms) and is spoken; measured at 4.5 it
    takes 5333 ms, so it is shortened at that rate and spoken again, alone."""
    run = await run_vbee(
        temporal_client,
        VbeeRun(
            slots=three_slots(words(10), words(24), words(10)),
            voice=FakeVbeeVoice(rate=4.5, sample_rate=6.0),
        ),
    )
    assert run.result.publish.outcome == "published"
    names = await run.scheduled()
    first_measure = names.index("clip_durations")
    assert names[first_measure : first_measure + 4] == [
        "clip_durations",
        SHORTEN,
        "write_slot_script",
        "plan_synthesis",
    ]
    assert names.count("clip_durations") == 1 and names.count(SHORTEN) == 1
    # At 4.5 a second a 4000 ms slot holds 18 syllables.
    (prompt,) = run.shortener.prompts
    assert ScriptedShortener.asked(prompt) == {1: 18}
    # Only the new line was spoken again; the others are cache hits.
    assert spoken(run) == {words(10): 1, words(24): 1, words(17, "gọn"): 1}
    assert run.result.script.script.script_key == "work/7001/r0/script.v2.json"
    assert run.result.dub.script_version == 2
    (reviewed,) = run.reviewed
    assert [(s.id, s.stretch_ratio, s.overflow_unresolved) for s in reviewed.segments] == [
        (0, 1.0, False),
        (1, 1.0, False),
        (2, 1.0, False),
    ]


async def test_a_voice_that_reads_real_lines_faster_than_the_sample_shortens_nothing_more(
    temporal_client: Client,
) -> None:
    run = await run_vbee(
        temporal_client,
        VbeeRun(
            slots=three_slots(words(10), words(24), words(10)),
            voice=FakeVbeeVoice(rate=6.0, sample_rate=5.4),
        ),
    )
    assert run.result.publish.outcome == "published"
    assert run.shortener.prompts == []
    assert (await run.scheduled()).count("write_slot_script") == 1


async def test_a_line_the_review_finds_too_long_is_shortened_alone_and_spoken_again(
    temporal_client: Client,
) -> None:
    """The last line reads slowly (2.5 a second): its estimate fits, its clip (4800 ms) does
    not. The review names it; exactly that line is shortened, at the rate it was read at,
    and the next round's review passes."""
    slow = words(12, SLOW_WORD)
    # Line 0 reads at the normal rate and fits; at the slow line's rate it would not, so a
    # request that asked every line overrunning at that rate would ask for it too.
    run = await run_vbee(temporal_client, VbeeRun(slots=three_slots(words(12), words(10), slow)))
    assert run.result.publish.outcome == "published"
    first, second = run.reviewed
    assert [s.id for s in first.segments if s.overflow_unresolved] == [2]
    assert not any(s.overflow_unresolved for s in second.segments)
    # The only request, after the first review, for line 2 alone: 10 syllables at 2.5.
    (prompt,) = run.shortener.prompts
    assert ScriptedShortener.asked(prompt) == {2: 10}
    names = await run.scheduled()
    after_review = names[names.index("review_dub") :]
    assert after_review == [
        "review_dub",
        "clip_durations",
        SHORTEN,
        "write_slot_script",
        "plan_synthesis",
        # The batches are cut as before; the lines already spoken are cache hits.
        "synthesize_segments",
        "synthesize_segments",
        "commit_dub",
        "assemble_audio",
        "layout_captions",
        "review_dub",
        "render",
        "publish",
    ]
    # The other lines were not spoken again; the round's work is in its own folder.
    assert spoken(run) == {words(12): 1, words(10): 1, slow: 1, words(9, "gọn"): 1}
    assert [x["round"] for x in run.reviewing.layouts] == [0, 1]
    assert run.result.script.script.script_key == "work/7001/r1/script.v2.json"
    assert run.result.dub.segments_key == "work/7001/r1/dub-segments.json"
    assert run.row()["review_rounds"] == 2 and run.reviewing.calls["render"] == 1
    # What is published is the render of the second round's layout.
    assert run.reviewing.renders == [f"work/7001/render-{run.reviewing.layouts[1]['sha']}.mp4"]


async def test_lines_still_too_long_after_the_one_rerun_go_to_a_person(
    temporal_client: Client,
) -> None:
    # The voice reads the shortener's word slowly too: the rewrite overruns again.
    run = await run_vbee(
        temporal_client,
        VbeeRun(
            slots=three_slots(words(10), words(10), words(12, SLOW_WORD)),
            voice=FakeVbeeVoice(slow={SLOW_WORD: 2.5, "gọn": 1.0}),
        ),
    )
    assert run.result.publish.outcome == "needs_human"
    names = await run.scheduled()
    assert names.count("review_dub") == 2 and names.count(SHORTEN) == 1
    assert names.count("render") == 1
    (reason,) = run.row()["record"]["reasons"]
    assert reason.startswith("the review did not pass after 2 round(s): dub: ")


async def test_lines_the_shortener_cannot_shorten_end_the_rounds_with_the_reason(
    temporal_client: Client,
) -> None:
    # Every rewrite has characters the caption font cannot show: the line keeps its text.
    run = await run_vbee(
        temporal_client,
        VbeeRun(
            slots=three_slots(words(10), words(10), words(12, SLOW_WORD)),
            shortener=ScriptedShortener(word="你好"),
        ),
    )
    assert run.result.publish.outcome == "needs_human"
    names = await run.scheduled()
    assert names.count(SHORTEN) == 1 and names.count(SHORTEN_REPAIR) == 1
    assert names.count("review_dub") == 1 and names.count("render") == 1
    assert names.count("write_slot_script") == 1
    reasons = run.row()["record"]["reasons"]
    assert reasons[-1] == "the lines of slots [2] could not be shortened to fit"
    assert run.result.slots.unfit_ids == (2,)


async def test_a_video_without_speech_is_published_with_its_music_alone(
    temporal_client: Client,
) -> None:
    """As an empty transcript does in the other flow: nothing is shortened or spoken, the
    track is the music, and the video goes through the review and out."""
    run = await run_vbee(temporal_client, VbeeRun(slots=three_slots()))
    assert run.result.publish.outcome == "published"
    assert await run.scheduled() == [
        *PREPARE,
        "write_slot_script",
        "plan_synthesis",
        "commit_dub",
        "assemble_audio",
        "layout_captions",
        "review_dub",
        "render",
        "publish",
    ]
    assert run.shortener.prompts == [] and spoken(run) == {}
    doc = DubSegmentsDocument.model_validate_json(
        run.store.get_bytes(run.result.dub.segments_key, produced_by="test")
    )
    assert doc.segments == ()


async def test_vbee_stt_failing_for_good_is_written_on_the_run_and_nothing_after_runs(
    temporal_client: Client,
) -> None:
    run = await run_vbee(
        temporal_client,
        VbeeRun(stt=FakeVbeeStt(PermanentProviderError("vbee stt: HTTP 401 UNAUTHORIZED"))),
    )
    assert isinstance(run.outcome, BaseException)
    # Not retried: a permanent failure costs one job.
    assert run.stt.calls == 1
    names = await run.scheduled()
    assert "plan_slots" not in names and names[-1] == "publish"
    assert run.row()["status"] == "failed" and run.row()["review_rounds"] == 0
    (reason,) = run.row()["record"]["reasons"]
    assert reason.startswith("PermanentProviderError: ")
    assert reason.endswith("vbee stt: HTTP 401 UNAUTHORIZED")


async def test_a_render_that_fails_for_good_fails_the_run_with_its_reason(
    temporal_client: Client,
) -> None:
    from temporalio.exceptions import ApplicationError

    run = await run_vbee(
        temporal_client,
        VbeeRun(
            reviewing=ReviewFakes(
                fail_on={
                    ("render", 1): ApplicationError(
                        "the render ran past its deadline",
                        type="RenderDeadline",
                        non_retryable=True,
                    )
                }
            )
        ),
    )
    assert isinstance(run.outcome, BaseException)
    assert run.row()["status"] == "failed" and run.row()["review_rounds"] == 1
    (reason,) = run.row()["record"]["reasons"]
    assert reason.startswith("RenderDeadline: ")


@pytest.mark.parametrize("shortened", [False, True])
async def test_the_configured_voice_id_is_spoken_with_but_never_in_the_history(
    temporal_client: Client, shortened: bool
) -> None:
    canary = "vbee-voice-CANARY-6c41e09b"
    long = words(30) if shortened else words(10)
    run = await run_vbee(
        temporal_client,
        VbeeRun(slots=three_slots(words(10), long, words(10)), voice_id=canary),
    )
    assert run.result.publish.outcome == "published"
    # The canary is really the voice everything was spoken in (the sample sentence too).
    assert run.voice.voices and set(run.voice.voices) == {canary}
    assert len(run.voice.voices) == 1 + len(spoken(run))
    history = await run.handle.fetch_history()
    assert canary not in history.to_json()
    # Nor in anything the run left in the store (documents, clip metadata).
    root = run.reviewing.root
    for path in root.rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes(), path


# --- the arithmetic the workflow decides with -----------------------------------------------


def test_a_synthesis_batch_is_given_time_for_each_piece_its_lines_are_read_in() -> None:
    from datetime import timedelta

    from localization.workflows.vbee_dubbing import SECONDS_PER_PIECE, synthesis_timeout

    one = words(10)
    # A line of three sentences is read as three pieces.
    three = "Xin chào các bạn. Hôm nay mình thử máy. Rất tốt nhé."
    base = synthesis_timeout([])
    assert synthesis_timeout([one]) - base == timedelta(seconds=SECONDS_PER_PIECE)
    assert synthesis_timeout([one, three]) - base == timedelta(seconds=4 * SECONDS_PER_PIECE)


def test_the_measured_rate_is_syllables_over_the_time_of_every_clip() -> None:
    from localization.contracts.vbee_flow import ClipDuration, ClipDurations
    from localization.workflows.vbee_dubbing import measured_rate, overrun_rate

    plan = three_slots(words(10), words(20), words(12))
    clips = ClipDurations(
        items=(
            ClipDuration(id=0, duration_ms=2000),
            ClipDuration(id=1, duration_ms=5000),
            ClipDuration(id=2, duration_ms=6000),
        )
    )
    assert measured_rate(plan, clips) == pytest.approx(42 / 13)
    assert measured_rate(three_slots(), ClipDurations(items=())) is None
    # Of the lines named, those longer than their 4000 ms slot; the slowest of their rates.
    assert overrun_rate(plan, clips, [1, 2]) == pytest.approx(2.0)
    assert overrun_rate(plan, clips, [0]) is None

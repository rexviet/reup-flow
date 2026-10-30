"""How the workflow splits a script's distinct texts into synthesis batches. Lines inside
a batch are spoken one after another, so the split is even and never leaves one of the
`tts` queue's concurrent slots idle while the other speaks a long batch."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import Client

from localization.contracts import LocalizationInput, LocalizationResult, SynthClip
from localization.workflows.video_localization import TTS_BATCH, TTS_PARALLEL, tts_batches
from tests.support.dub_fakes import DubFakes, by_name
from tests.workers.test_queue_concurrency import max_overlap
from tests.workflows.test_dubbing_stage import run_workflow


def _hashes(n: int) -> list[str]:
    return [f"{i:064x}" for i in range(n)]


@pytest.mark.parametrize(
    ("n", "sizes"),
    [
        (1, [1]),
        (2, [1, 1]),
        (7, [4, 3]),
        (10, [5, 5]),
        (12, [6, 6]),
        (20, [10, 10]),
        (21, [7, 7, 7]),
        (25, [9, 8, 8]),
        (40, [10, 10, 10, 10]),
    ],
)
def test_the_hashes_are_split_into_even_batches_in_their_order(n: int, sizes: list[int]) -> None:
    hashes = _hashes(n)
    batches = tts_batches(hashes)
    assert [len(b) for b in batches] == sizes
    assert [h for b in batches for h in b] == hashes


def test_no_batch_is_larger_than_the_cap_and_at_least_the_parallel_slots_are_used() -> None:
    for n in range(1, 101):
        batches = tts_batches(_hashes(n))
        sizes = [len(b) for b in batches]
        assert max(sizes) <= TTS_BATCH, n
        assert max(sizes) - min(sizes) <= 1, n
        assert len(batches) >= min(TTS_PARALLEL, n), n


def test_no_hashes_make_no_batches() -> None:
    assert tts_batches([]) == []


@dataclass
class TimedDub(DubFakes):
    """The dubbing fakes, with when each synthesis batch ran."""

    synth_spans: list[tuple[float, float]] = field(default_factory=list)

    def activities(self) -> list[Callable]:
        acts = super().activities()
        inner = by_name(acts)["synthesize_segments"]

        @activity.defn(name="synthesize_segments")
        async def synthesize_segments(
            spec: LocalizationInput, engine: str | None, script_key: str, hashes: list[str]
        ) -> list[SynthClip]:
            entered = time.monotonic()
            try:
                return await inner(spec, engine, script_key, hashes)
            finally:
                self.synth_spans.append((entered, time.monotonic()))

        return [a if a is not inner else synthesize_segments for a in acts]


async def test_a_seven_line_script_is_spoken_as_two_batches_at_once(
    temporal_client: Client,
) -> None:
    dubbing = TimedDub(hashes=7, batch_seconds=0.5)
    _, result = await run_workflow(temporal_client, dubbing, tts_limit=TTS_PARALLEL)
    assert isinstance(result, LocalizationResult), result
    # Two passes (the Localizer's script, then the dubbed one), two batches each.
    assert len(dubbing.batches) == 4
    for pass_batches in (dubbing.batches[:2], dubbing.batches[2:]):
        assert sorted(len(b) for b in pass_batches) == [3, 4]
        assert sorted(h for b in pass_batches for h in b) == _hashes(7)
    first_pass = sorted(dubbing.synth_spans)[:2]
    assert max_overlap(first_pass) == 2, dubbing.synth_spans

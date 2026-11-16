"""The `ingest` worker role: the separation on its own queue, for a worker on the host where
Demucs can use the GPU. The `media` role keeps serving ingest on its own queue, so a run that
began before the queue existed (and asks `media`) is still served."""

from types import SimpleNamespace

from localization.config import Settings
from localization.workers.run import queue_specs
from localization.workflows.video_localization import INGEST_QUEUE, WATERMARK_QUEUE


def activity_names(spec) -> set[str]:
    return {fn.__temporal_activity_definition.name for fn in spec.activities}  # type: ignore[attr-defined]


def test_the_ingest_role_serves_the_separation_and_the_watermark_search() -> None:
    providers = SimpleNamespace(demucs=object())
    spec, marks = queue_specs("ingest", Settings(), None, providers)  # type: ignore[arg-type]
    assert spec.name == INGEST_QUEUE == "ingest"
    assert activity_names(spec) == {"ingest"}
    assert marks.name == WATERMARK_QUEUE == "watermarks"
    assert activity_names(marks) == {"detect_watermarks"} and marks.limit == 1


def test_the_watermark_search_uses_the_threads_the_settings_give() -> None:
    providers = SimpleNamespace(demucs=object())
    _, marks = queue_specs("ingest", Settings(watermark_threads=6), None, providers)  # type: ignore[arg-type]
    (activity,) = marks.activities
    assert activity.__self__._threads == 6  # type: ignore[attr-defined]


def test_one_separation_runs_at_a_time_unless_the_settings_say_more() -> None:
    providers = SimpleNamespace(demucs=object())
    default, _ = queue_specs("ingest", Settings(), None, providers)  # type: ignore[arg-type]
    more, _ = queue_specs("ingest", Settings(ingest_concurrency=2), None, providers)  # type: ignore[arg-type]
    assert (default.limit, more.limit) == (1, 2)

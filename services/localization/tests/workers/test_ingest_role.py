"""The `ingest` worker role: the separation on its own queue, for a worker on the host where
Demucs can use the GPU. The `media` role keeps serving ingest on its own queue, so a run that
began before the queue existed (and asks `media`) is still served."""

from types import SimpleNamespace

from localization.config import Settings
from localization.workers.run import queue_specs
from localization.workflows.video_localization import INGEST_QUEUE


def activity_names(spec) -> set[str]:
    return {fn.__temporal_activity_definition.name for fn in spec.activities}  # type: ignore[attr-defined]


def test_the_ingest_role_serves_only_the_ingest_queue_with_only_the_ingest_activity() -> None:
    providers = SimpleNamespace(demucs=object())
    (spec,) = queue_specs("ingest", Settings(), None, providers)  # type: ignore[arg-type]
    assert spec.name == INGEST_QUEUE == "ingest"
    assert activity_names(spec) == {"ingest"}


def test_one_separation_runs_at_a_time_unless_the_settings_say_more() -> None:
    providers = SimpleNamespace(demucs=object())
    (default,) = queue_specs("ingest", Settings(), None, providers)  # type: ignore[arg-type]
    (more,) = queue_specs("ingest", Settings(ingest_concurrency=2), None, providers)  # type: ignore[arg-type]
    assert (default.limit, more.limit) == (1, 2)

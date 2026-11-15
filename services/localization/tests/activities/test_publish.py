"""How a run ends: what is copied where, what is never copied to `output/`, and the
row the run leaves, against a real Postgres with the real migration."""

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities.publish import PublishActivity, RunStore
from localization.contracts import PublishRequest
from localization.storage.object_store import ObjectStore, output_key, review_key
from tests.support.file_s3 import FileS3
from tests.support.inputs import make_input
from tests.support.review_fakes import MemoryRuns, issue, verdict
from tests.support.scratch_db import scratch_database

CORE = (
    Path(__file__).resolve().parents[2] / "migrations" / "261026-localization-core.sql"
).read_text()
LLM_COST = (Path(__file__).resolve().parents[2] / "migrations" / "261027-llm-cost.sql").read_text()
LLM_PROVIDER = (
    Path(__file__).resolve().parents[2] / "migrations" / "261028-llm-call-provider.sql"
).read_text()
RENDER = "work/7001/render-" + "a" * 64 + ".mp4"
SPEC = make_input("7001", 3)


@pytest.fixture
def db() -> Iterator[tuple[str, psycopg.Connection]]:
    """The real migration, with every session running as `localization`, who the worker is."""
    for info, conn in scratch_database(CORE, LLM_COST, LLM_PROVIDER):
        yield f"{info} options='-c role=localization'", conn


@pytest.fixture
def store(tmp_path: Path) -> ObjectStore:
    store = ObjectStore(FileS3(tmp_path), "reup-media")
    store.put_bytes(RENDER, b"the-mp4")
    return store


def publisher(store: ObjectStore, runs) -> PublishActivity:
    return PublishActivity(store, runs)


async def publish(act: PublishActivity, request: PublishRequest):
    return await ActivityEnvironment().run(act.publish, SPEC, request)


def present(store: ObjectStore, key: str) -> bool:
    return store.exists(key)


PASSED = PublishRequest(outcome="published", render_key=RENDER, verdict=verdict(), review_rounds=1)


# --- published --------------------------------------------------------------------------


async def test_a_run_that_passed_is_copied_to_output_under_its_video_id(store: ObjectStore) -> None:
    runs = MemoryRuns()
    result = await publish(publisher(store, runs), PASSED)
    assert (result.outcome, result.output_key, result.review_key) == (
        "published",
        output_key("7001"),
        None,
    )
    assert store.get_bytes(output_key("7001"), produced_by="t") == b"the-mp4"
    assert not present(store, review_key("7001", 3))
    row = runs.rows[("7001", 3)]
    assert (row["status"], row["output"], row["review"], row["needs_human"]) == (
        "published",
        output_key("7001"),
        None,
        False,
    )


@pytest.mark.parametrize(
    "request_",
    [
        PublishRequest(outcome="published", render_key=RENDER, verdict=None),
        PublishRequest(
            outcome="published", render_key=RENDER, verdict=verdict(issue("dub", segment_id=1))
        ),
        PublishRequest(
            outcome="published",
            render_key=RENDER,
            verdict=verdict(),
            reasons=("a person must look",),
        ),
        PublishRequest(outcome="published", verdict=verdict()),
    ],
    ids=["no verdict", "a failing verdict", "something flagged", "no render"],
)
async def test_nothing_that_did_not_pass_is_ever_written_to_output(
    store: ObjectStore, request_: PublishRequest
) -> None:
    """Whatever the caller meant: `output/` is what the world sees, so the activity checks."""
    runs = MemoryRuns()
    with pytest.raises(ApplicationError) as info:
        await publish(publisher(store, runs), request_)
    assert (info.value.type, info.value.non_retryable) == ("PublishRefused", True)
    assert not present(store, output_key("7001"))
    assert runs.rows == {}


# --- needs a person ---------------------------------------------------------------------


async def test_a_run_for_a_person_keeps_its_render_under_review_by_generation(
    store: ObjectStore,
) -> None:
    runs = MemoryRuns()
    request = PublishRequest(
        outcome="needs_human",
        render_key=RENDER,
        reasons=("the Localizer flagged segments [0]",),
        review_rounds=2,
        verdict=verdict(issue("dub", "rushed", segment_id=1)),
    )
    result = await publish(publisher(store, runs), request)
    assert (result.outcome, result.output_key, result.review_key) == (
        "needs_human",
        None,
        review_key("7001", 3),
    )
    assert review_key("7001", 3) == "review/7001-g3.mp4"
    assert store.get_bytes("review/7001-g3.mp4", produced_by="t") == b"the-mp4"
    assert not present(store, output_key("7001"))
    row = runs.rows[("7001", 3)]
    assert (row["status"], row["needs_human"], row["review_rounds"]) == ("needs_human", True, 2)
    assert row["record"]["reasons"] == ["the Localizer flagged segments [0]"]
    assert row["record"]["verdict"]["pass"] is False
    assert row["record"]["verdict"]["issues"][0]["segment_id"] == 1


async def test_a_run_for_a_person_needs_the_render_it_is_about(store: ObjectStore) -> None:
    with pytest.raises(ApplicationError, match="needs the render"):
        await publish(publisher(store, MemoryRuns()), PublishRequest(outcome="needs_human"))


async def test_the_render_of_each_generation_is_kept_apart(store: ObjectStore) -> None:
    act = publisher(store, MemoryRuns())
    request = PublishRequest(outcome="needs_human", render_key=RENDER, reasons=("x",))
    await publish(act, request)
    store.put_bytes(RENDER, b"another")
    await ActivityEnvironment().run(act.publish, make_input("7001", 4), request)
    assert store.get_bytes("review/7001-g3.mp4", produced_by="t") == b"the-mp4"
    assert store.get_bytes("review/7001-g4.mp4", produced_by="t") == b"another"


# --- failed -----------------------------------------------------------------------------


async def test_a_failed_run_leaves_a_row_with_its_reason_and_copies_nothing(
    store: ObjectStore,
) -> None:
    runs = MemoryRuns()
    request = PublishRequest(
        outcome="failed", reasons=("UncoveredGlyphs: segment 3: 你",), review_rounds=0
    )
    result = await publish(publisher(store, runs), request)
    assert (result.outcome, result.output_key, result.review_key) == ("failed", None, None)
    assert store.get_bytes(RENDER, produced_by="t") == b"the-mp4"
    assert not present(store, output_key("7001")) and not present(store, review_key("7001", 3))
    row = runs.rows[("7001", 3)]
    assert (row["status"], row["needs_human"]) == ("failed", False)
    assert row["record"]["reasons"] == ["UncoveredGlyphs: segment 3: 你"]


async def test_a_failed_run_has_no_render_to_keep(store: ObjectStore) -> None:
    with pytest.raises(ApplicationError, match="no render"):
        await publish(
            publisher(store, MemoryRuns()), PublishRequest(outcome="failed", render_key=RENDER)
        )


# --- a product advertisement, not localized ----------------------------------------------


async def test_a_skipped_advertisement_leaves_a_row_and_copies_nothing(store: ObjectStore) -> None:
    runs = MemoryRuns()
    result = await publish(publisher(store, runs), PublishRequest(outcome="skipped_ad"))
    assert (result.outcome, result.output_key, result.review_key) == ("skipped_ad", None, None)
    assert not present(store, output_key("7001")) and not present(store, review_key("7001", 3))
    row = runs.rows[("7001", 3)]
    assert (row["status"], row["needs_human"], row["review_rounds"]) == ("skipped_ad", False, 0)
    assert (row["output"], row["review"]) == (None, None)
    assert row["record"] == {"reasons": [], "verdict": None}


async def test_a_skipped_advertisement_has_no_render_to_keep(store: ObjectStore) -> None:
    runs = MemoryRuns()
    with pytest.raises(ApplicationError, match="no render"):
        await publish(
            publisher(store, runs), PublishRequest(outcome="skipped_ad", render_key=RENDER)
        )
    assert runs.rows == {} and not present(store, review_key("7001", 3))


async def test_a_skipped_advertisement_is_a_row_the_real_table_takes(
    store: ObjectStore, db: tuple[str, psycopg.Connection]
) -> None:
    """`status` is free text with no CHECK on its values: the new one is stored as it is."""
    info, conn = db
    await publish(publisher(store, RunStore(info)), PublishRequest(outcome="skipped_ad"))
    status, out, review, needs_human, rounds, workflow_id, record = _row(conn)
    assert (status, out, review, needs_human, rounds) == ("skipped_ad", None, None, False, 0)
    assert workflow_id and record == {"reasons": [], "verdict": None}


# --- a video without a voice, published as it is --------------------------------------------


SOURCE = b"the-original-mp4"


async def test_an_original_is_copied_to_output_as_it_is_and_leaves_its_row(
    store: ObjectStore,
) -> None:
    store.put_bytes(SPEC.video_key, SOURCE)
    runs = MemoryRuns()
    result = await publish(publisher(store, runs), PublishRequest(outcome="published_original"))
    assert (result.outcome, result.output_key, result.review_key) == (
        "published_original",
        output_key("7001"),
        None,
    )
    assert store.get_bytes(output_key("7001"), produced_by="t") == SOURCE
    # The source stays where it was, and nothing is kept for a person.
    assert store.get_bytes(SPEC.video_key, produced_by="t") == SOURCE
    assert not present(store, review_key("7001", 3))
    row = runs.rows[("7001", 3)]
    assert (row["status"], row["needs_human"], row["review_rounds"]) == (
        "published_original",
        False,
        0,
    )
    assert (row["output"], row["review"]) == (output_key("7001"), None)
    assert row["record"] == {"reasons": [], "verdict": None}


@pytest.mark.parametrize(
    "request_",
    [
        PublishRequest(outcome="published_original", render_key=RENDER),
        PublishRequest(outcome="published_original", verdict=verdict()),
        PublishRequest(outcome="published_original", reasons=("music only",)),
    ],
    ids=["a-render", "a-verdict", "reasons"],
)
async def test_an_original_is_published_only_for_a_run_that_made_nothing(
    store: ObjectStore, request_: PublishRequest
) -> None:
    store.put_bytes(SPEC.video_key, SOURCE)
    runs = MemoryRuns()
    with pytest.raises(ApplicationError) as caught:
        await publish(publisher(store, runs), request_)
    assert caught.value.type == "PublishRefused" and caught.value.non_retryable
    assert runs.rows == {} and not present(store, output_key("7001"))


async def test_a_missing_source_is_a_lost_download_not_a_publish(store: ObjectStore) -> None:
    runs = MemoryRuns()
    with pytest.raises(ApplicationError) as caught:
        await publish(publisher(store, runs), PublishRequest(outcome="published_original"))
    assert caught.value.type == "ArtifactMissingError"
    assert caught.value.details[0] == "download"
    assert runs.rows == {} and not present(store, output_key("7001"))


async def test_a_published_original_is_a_row_the_real_table_takes(
    store: ObjectStore, db: tuple[str, psycopg.Connection]
) -> None:
    """`status` is free text with no CHECK on its values: the new one is stored as it is."""
    store.put_bytes(SPEC.video_key, SOURCE)
    info, conn = db
    await publish(publisher(store, RunStore(info)), PublishRequest(outcome="published_original"))
    status, out, review, needs_human, rounds, workflow_id, record = _row(conn)
    assert (status, out, review, needs_human, rounds) == (
        "published_original",
        output_key("7001"),
        None,
        False,
        0,
    )
    assert workflow_id and record == {"reasons": [], "verdict": None}


# --- a file without a video stream, skipped -----------------------------------------------


async def test_a_file_without_a_picture_leaves_a_row_and_copies_nothing(
    store: ObjectStore,
) -> None:
    store.put_bytes(SPEC.video_key, SOURCE)
    runs = MemoryRuns()
    result = await publish(publisher(store, runs), PublishRequest(outcome="skipped_no_video"))
    assert (result.outcome, result.output_key, result.review_key) == (
        "skipped_no_video",
        None,
        None,
    )
    assert not present(store, output_key("7001")) and not present(store, review_key("7001", 3))
    row = runs.rows[("7001", 3)]
    assert (row["status"], row["needs_human"], row["review_rounds"]) == (
        "skipped_no_video",
        False,
        0,
    )
    assert (row["output"], row["review"]) == (None, None)
    assert row["record"] == {"reasons": [], "verdict": None}


async def test_a_file_without_a_picture_has_no_render_to_keep(store: ObjectStore) -> None:
    runs = MemoryRuns()
    with pytest.raises(ApplicationError, match="no render"):
        await publish(
            publisher(store, runs), PublishRequest(outcome="skipped_no_video", render_key=RENDER)
        )
    assert runs.rows == {} and not present(store, output_key("7001"))


async def test_a_skipped_file_without_a_picture_is_a_row_the_real_table_takes(
    store: ObjectStore, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    await publish(publisher(store, RunStore(info)), PublishRequest(outcome="skipped_no_video"))
    status, out, review, needs_human, rounds, workflow_id, record = _row(conn)
    assert (status, out, review, needs_human, rounds) == ("skipped_no_video", None, None, False, 0)
    assert workflow_id and record == {"reasons": [], "verdict": None}


async def test_a_missing_render_is_a_lost_artifact_not_a_publish(tmp_path: Path) -> None:
    empty = ObjectStore(FileS3(tmp_path), "reup-media")
    runs = MemoryRuns()
    with pytest.raises(ApplicationError) as info:
        await publish(publisher(empty, runs), PASSED)
    assert info.value.type == "ArtifactMissingError"
    assert runs.rows == {}


# --- idempotent, and the row in a real database -----------------------------------------


async def test_the_same_publish_twice_leaves_the_same_state(store: ObjectStore) -> None:
    runs = MemoryRuns()
    act = publisher(store, runs)
    first = await publish(act, PASSED)
    second = await publish(act, PASSED)
    assert first == second
    assert len(runs.rows) == 1
    assert store.get_bytes(output_key("7001"), produced_by="t") == b"the-mp4"


def _row(conn: psycopg.Connection) -> tuple:
    return conn.execute(
        "SELECT status, output_key, review_key, needs_human, review_rounds, workflow_id, "
        "verdict FROM localization.runs WHERE aweme_id = '7001' AND generation = 3"
    ).fetchone()


async def test_the_row_is_one_per_video_and_generation_in_a_real_database(
    store: ObjectStore, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    act = publisher(store, RunStore(info))
    await publish(act, PASSED)
    await publish(act, PASSED)
    count = conn.execute("SELECT count(*) FROM localization.runs").fetchone()[0]
    assert count == 1
    status, out, review, needs_human, rounds, workflow_id, record = _row(conn)
    assert (status, out, review, needs_human, rounds) == (
        "published",
        output_key("7001"),
        None,
        False,
        1,
    )
    assert workflow_id and record["verdict"]["pass"] is True and record["reasons"] == []


async def test_a_later_outcome_of_the_same_generation_replaces_the_row(
    store: ObjectStore, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    act = publisher(store, RunStore(info))
    await publish(act, PublishRequest(outcome="failed", reasons=("boom",)))
    await publish(act, PublishRequest(outcome="needs_human", render_key=RENDER, reasons=("look",)))
    assert conn.execute("SELECT count(*) FROM localization.runs").fetchone()[0] == 1
    status, out, review, needs_human, *_ = _row(conn)
    assert (status, out, review, needs_human) == (
        "needs_human",
        None,
        review_key("7001", 3),
        True,
    )


async def test_two_generations_of_a_video_are_two_rows(
    store: ObjectStore, db: tuple[str, psycopg.Connection]
) -> None:
    info, conn = db
    act = publisher(store, RunStore(info))
    await publish(act, PASSED)
    await ActivityEnvironment().run(act.publish, make_input("7001", 4), PASSED)
    rows = conn.execute("SELECT generation FROM localization.runs ORDER BY 1").fetchall()
    assert rows == [(3,), (4,)]


@pytest.mark.parametrize("outcome", ["ready_for_review", "needs_human"])
async def test_review_project_pointer_is_stored_in_the_real_table(
    review_store, tmp_path, db, outcome
):
    from tests.support.review_project import project_inputs

    info, conn = db
    migration = Path(__file__).resolve().parents[2] / "migrations/261108-review-project-key.sql"
    conn.execute(migration.read_text())
    builder, spec, dub, ingest_, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest_, subs, None)
    act = PublishActivity(review_store, RunStore(info))
    request = PublishRequest(outcome=outcome, project_key=ref.project_key, verdict=verdict())
    for _ in range(2):
        result = await ActivityEnvironment().run(act.publish, spec, request)
    assert result.project_key == ref.project_key and result.output_key is None
    assert conn.execute(
        "SELECT status,project_key,output_key,review_key FROM localization.runs"
    ).fetchall() == [(outcome, ref.project_key, None, None)]

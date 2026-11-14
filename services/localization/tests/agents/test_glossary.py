"""The glossary against a real Postgres: first writer wins, a run's writes are
capped, and a write the guard refuses leaves no row."""

import asyncio
import json
import re
import unicodedata
from pathlib import Path

import psycopg
import pytest

from localization.activities.glossary import (
    MAX_WRITES_PER_RUN,
    TERM_MAX_CHARS,
    GlossaryStore,
    check_term,
)


def _rows(conn: psycopg.Connection) -> list[tuple]:
    """Rows with the channel their collection belongs to."""
    return conn.execute(
        "SELECT c.external_id, g.term_zh, g.term_vi, g.source_aweme_id, g.workflow_id "
        "FROM localization.glossary g JOIN public.collections c ON c.id = g.collection_id "
        "WHERE c.kind = 'channel' ORDER BY g.term_zh"
    ).fetchall()


async def _save(store: GlossaryStore, term_zh: str, term_vi: str, **kw):
    params = {
        "channel_id": "chan_1",
        "note": None,
        "aweme_id": "7001",
        "workflow_id": "video-7001-g1",
    }
    return await store.upsert(term_zh=term_zh, term_vi=term_vi, **{**params, **kw})


async def test_the_same_term_written_twice_is_one_row_and_the_first_value_stays(
    glossary_db,
) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    first = await _save(store, "苹果", "Apple")
    second = await _save(store, "苹果", "quả táo", aweme_id="7002", workflow_id="video-7002-g1")
    assert first.status == "inserted"
    assert second.status == "exists"
    assert _rows(conn) == [("chan_1", "苹果", "Apple", "7001", "video-7001-g1")]


async def test_the_reply_to_a_repeat_tells_the_agent_which_term_to_use(glossary_db) -> None:
    info, _ = glossary_db
    store = GlossaryStore(info)
    await _save(store, "苹果", "Apple")
    again = await _save(store, "苹果", "quả táo")
    assert again.existing == "Apple"
    assert "Apple" not in again.message


async def test_a_retried_write_after_the_row_landed_adds_nothing(glossary_db) -> None:
    """A tool activity can be retried after its INSERT committed but before the
    worker reported back; the retry must find the row, not add a second."""
    info, conn = glossary_db
    store = GlossaryStore(info)
    for _ in range(3):
        await _save(store, "华为", "Huawei")
    assert len(_rows(conn)) == 1


async def test_two_writers_of_one_term_at_once_leave_one_row(glossary_db) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    outcomes = await asyncio.gather(
        *(
            _save(store, "小米", f"Xiaomi {chr(97 + i)}", workflow_id=f"video-{i}-g1")
            for i in range(8)
        )
    )
    assert sorted(o.status for o in outcomes) == ["exists"] * 7 + ["inserted"]
    assert len(_rows(conn)) == 1


@pytest.mark.parametrize(
    ("term_vi", "why"),
    [
        ("x" * (TERM_MAX_CHARS + 1), "longer"),
        ("ghé shop-xyz.vn", "domain"),
        ("vào https://shop.example.com", "URL"),
        ("gọi 0912345678", "phone"),
        ("mail a@b.com", "e-mail"),
        ("iPhone 16", "digits"),
        ("", "empty"),
        ("dòng một\ndòng hai", "line"),
    ],
)
async def test_a_term_the_guard_refuses_leaves_no_row(glossary_db, term_vi: str, why: str) -> None:
    info, conn = glossary_db
    outcome = await _save(GlossaryStore(info), "手机", term_vi)
    assert outcome.status == "rejected", why
    assert outcome.message.startswith("not saved")
    assert _rows(conn) == []


async def test_digits_the_chinese_term_has_are_allowed(glossary_db) -> None:
    info, conn = glossary_db
    outcome = await _save(GlossaryStore(info), "iPhone16", "iPhone 16")
    assert outcome.status == "inserted"
    assert len(_rows(conn)) == 1


async def test_a_note_with_a_link_is_refused_too(glossary_db) -> None:
    info, conn = glossary_db
    outcome = await _save(GlossaryStore(info), "手机", "điện thoại", note="xem shop-xyz.vn")
    assert outcome.status == "rejected"
    assert _rows(conn) == []


async def test_a_run_can_add_only_so_many_terms(glossary_db) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    for i in range(MAX_WRITES_PER_RUN):
        assert (await _save(store, f"词{i}", f"từ {chr(97 + i)}")).status == "inserted"
    over = await _save(store, "多余", "thừa")
    assert over.status == "limit"
    assert len(_rows(conn)) == MAX_WRITES_PER_RUN
    # Another run is not held to this one's count, and a term that exists is still
    # reported as existing rather than as over the limit.
    assert (await _save(store, "别的", "khác", workflow_id="video-7002-g1")).status == "inserted"
    assert (await _save(store, "词0", "x")).status == "exists"


async def test_lookup_is_per_channel_and_by_term(glossary_db) -> None:
    info, _ = glossary_db
    store = GlossaryStore(info)
    await _save(store, "苹果", "Apple")
    await _save(store, "华为", "Huawei")
    await _save(store, "苹果", "Táo khuyết", channel_id="chan_2")
    some = await store.lookup("chan_1", ["苹果"])
    assert [(r.term_zh, r.term_vi) for r in some] == [("苹果", "Apple")]
    everything = await store.lookup("chan_1", None)
    assert {r.term_zh for r in everything} == {"苹果", "华为"}
    assert await store.lookup("chan_3", None) == []


async def test_terms_of_a_channel_land_in_its_channel_collection(glossary_db) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    await _save(store, "苹果", "Apple", channel_id="chan_2")
    assert conn.execute(
        "SELECT c.kind, c.external_id FROM localization.glossary g "
        "JOIN public.collections c ON c.id = g.collection_id"
    ).fetchall() == [("channel", "chan_2")]


async def test_a_channel_without_a_collection_is_an_error_not_an_orphan_term(glossary_db) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    with pytest.raises(psycopg.errors.NotNullViolation):
        await _save(store, "苹果", "Apple", channel_id="chan_3")
    assert conn.execute("SELECT count(*) FROM localization.glossary").fetchone() == (0,)


def test_check_term_accepts_an_ordinary_term() -> None:
    assert check_term("苹果", "Apple", "hãng điện thoại") is None


# The dashboard checks a term a person sets with its own copy of `check_term`
# (services/downloader/src/collections/glossary-term.ts). Both test suites read
# this one file, so the two copies cannot drift apart unnoticed.
_SHARED_CASES = json.loads(
    (Path(__file__).resolve().parents[4] / "contracts" / "glossary-term-cases.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", _SHARED_CASES, ids=[c["name"] for c in _SHARED_CASES])
def test_check_term_matches_the_shared_cases(case: dict) -> None:
    assert check_term(case["term_zh"], case["term_vi"], case["note"]) == case["reason"]


def _unassigned_ranges() -> list[tuple[int, int]]:
    """Python's own unassigned (Cn) code points, as inclusive ranges."""
    ranges: list[tuple[int, int]] = []
    start = None
    for cp in range(0x110000):
        unassigned = unicodedata.category(chr(cp)) == "Cn"
        if unassigned and start is None:
            start = cp
        elif not unassigned and start is not None:
            ranges.append((start, cp - 1))
            start = None
    if start is not None:
        ranges.append((start, 0x10FFFF))
    return ranges


def test_the_dashboard_copy_knows_which_code_points_this_python_leaves_unassigned() -> None:
    """The dashboard's check runs on Node's newer Unicode and treats these code points
    the way this Python does. A Python upgrade changes the set, and this fails until the
    table in python-unassigned.ts is regenerated."""
    table = (
        Path(__file__).resolve().parents[3]
        / "downloader"
        / "src"
        / "collections"
        / "python-unassigned.ts"
    ).read_text(encoding="utf-8")
    version = re.search(r"PYTHON_UNICODE_VERSION = '([^']+)'", table)
    assert version is not None and version.group(1) == unicodedata.unidata_version
    body = "".join(re.findall(r"^  '(.*)'(?: \+|;)$", table, re.MULTILINE))
    listed = [
        (int(a, 16), int(b or a, 16))
        for a, b in re.findall(r"\\u\{([0-9a-f]+)\}(?:-\\u\{([0-9a-f]+)\})?", body)
    ]
    assert listed == _unassigned_ranges()

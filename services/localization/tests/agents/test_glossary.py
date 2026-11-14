"""The glossary against a real Postgres: first writer wins, a run's writes are
capped, and a write the guard refuses leaves no row."""

import asyncio
import json
import logging
import re
import unicodedata
from pathlib import Path

import psycopg
import pytest

from localization.activities.glossary import (
    LOOKUP_LIMIT,
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
        "collection_id": None,
        "channel_id": "chan_1",
        "note": None,
        "aweme_id": "7001",
        "workflow_id": "video-7001-g1",
    }
    return await store.upsert(term_zh=term_zh, term_vi=term_vi, **{**params, **kw})


async def _lookup(store: GlossaryStore, terms, *, collection_id=None, channel_id="chan_1"):
    return await store.lookup(collection_id=collection_id, channel_id=channel_id, terms=terms)


def _mix(conn: psycopg.Connection, mix_id: str, channel: str = "chan_1") -> str:
    """A 合集 collection of `channel`; returns its id."""
    return conn.execute(
        "INSERT INTO collections (kind, external_id, sec_user_id, name) "
        "VALUES ('mix', %s, %s, %s) RETURNING id::text",
        (mix_id, channel, mix_id),
    ).fetchone()[0]


def _dashboard_term(conn, collection_id: str, term_zh: str, term_vi: str, note=None) -> None:
    """A term a person set on the dashboard (phase 4 writes them this way)."""
    conn.execute(
        "INSERT INTO localization.glossary "
        "(collection_id, term_zh, term_vi, note, source_aweme_id, workflow_id) "
        "VALUES (%s, %s, %s, %s, 'dashboard', 'dashboard')",
        (collection_id, term_zh, term_vi, note),
    )


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
    some = await _lookup(store, ["苹果"])
    assert [(r.term_zh, r.term_vi) for r in some] == [("苹果", "Apple")]
    everything = await _lookup(store, None)
    assert {r.term_zh for r in everything} == {"苹果", "华为"}
    assert await _lookup(store, None, channel_id="chan_3") == []


async def test_terms_of_a_channel_land_in_its_channel_collection(glossary_db) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    await _save(store, "苹果", "Apple", channel_id="chan_2")
    assert conn.execute(
        "SELECT c.kind, c.external_id FROM localization.glossary g "
        "JOIN public.collections c ON c.id = g.collection_id"
    ).fetchall() == [("channel", "chan_2")]


async def test_a_channel_without_a_collection_stores_no_orphan_term(glossary_db) -> None:
    """An old input whose channel has no channel collection: nothing is stored, and the
    agent is told so instead of the activity failing on a write a retry cannot fix."""
    info, conn = glossary_db
    store = GlossaryStore(info)
    outcome = await _save(store, "苹果", "Apple", channel_id="chan_3")
    assert (outcome.status, outcome.message) == (
        "rejected",
        "not saved: this video's channel has no collection",
    )
    assert conn.execute("SELECT count(*) FROM localization.glossary").fetchone() == (0,)


async def test_a_write_for_a_collection_that_does_not_exist_is_refused_by_the_key(
    glossary_db, caplog
) -> None:
    info, conn = glossary_db
    gone = "0b6f3c1e-2d4a-4f8e-9a51-7c2e8d9f0a13"
    with caplog.at_level(logging.WARNING, logger="localization.activities.glossary"):
        outcome = await _save(GlossaryStore(info), "苹果", "Apple", collection_id=gone)
    assert (outcome.status, outcome.message) == (
        "rejected",
        "not saved: this video's collection no longer exists",
    )
    # An operator can see that this run's glossary went nowhere.
    assert any(gone in r.getMessage() for r in caplog.records), caplog.records
    assert conn.execute("SELECT count(*) FROM localization.glossary").fetchone() == (0,)
    # The foreign key, not this code, is what refuses it: the same row written directly
    # by the worker role fails the same way.
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        async with await psycopg.AsyncConnection.connect(info, autocommit=True) as worker:
            await worker.execute(
                "INSERT INTO localization.glossary (collection_id, term_zh, term_vi, "
                "source_aweme_id, workflow_id) VALUES (%s, 'x', 'y', '1', 'w')",
                (gone,),
            )


async def test_terms_of_one_collection_never_reach_another(glossary_db) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    first, second = _mix(conn, "mix_a"), _mix(conn, "mix_b")
    assert (await _save(store, "薛珍珠", "Tiết Trân Châu", collection_id=first)).status == (
        "inserted"
    )
    assert await _lookup(store, None, collection_id=second) == []
    assert await _lookup(store, ["薛珍珠"], collection_id=second) == []
    # Nor the channel the two collections belong to.
    assert await _lookup(store, None) == []
    found = await _lookup(store, ["薛珍珠"], collection_id=first)
    assert [(r.term_zh, r.term_vi) for r in found] == [("薛珍珠", "Tiết Trân Châu")]
    # The same term means something else in the second collection: both are kept.
    assert (await _save(store, "薛珍珠", "Châu Nhi", collection_id=second)).status == "inserted"
    assert [r.term_vi for r in await _lookup(store, None, collection_id=second)] == ["Châu Nhi"]


async def test_a_run_with_a_collection_uses_it_and_not_its_channel(glossary_db) -> None:
    info, conn = glossary_db
    store = GlossaryStore(info)
    mix = _mix(conn, "mix_a")
    await _save(store, "苹果", "Apple")
    outcome = await _save(store, "苹果", "Táo", collection_id=mix)
    assert outcome.status == "inserted"
    assert conn.execute(
        "SELECT c.kind, g.term_vi FROM localization.glossary g "
        "JOIN public.collections c ON c.id = g.collection_id ORDER BY c.kind"
    ).fetchall() == [("channel", "Apple"), ("mix", "Táo")]


async def test_an_input_from_before_collections_reads_its_channel_collection(glossary_db) -> None:
    """A run started before the migration (or "Go on" on one) carries only its channel."""
    info, conn = glossary_db
    channel_collection = conn.execute(
        "SELECT id::text FROM collections WHERE kind = 'channel' AND external_id = 'chan_2'"
    ).fetchone()[0]
    _dashboard_term(conn, channel_collection, "华为", "Huawei")
    found = await _lookup(GlossaryStore(info), None, channel_id="chan_2")
    assert [(r.term_zh, r.term_vi) for r in found] == [("华为", "Huawei")]


async def test_the_agent_never_overwrites_a_term_a_person_set(glossary_db) -> None:
    info, conn = glossary_db
    mix = _mix(conn, "mix_a")
    _dashboard_term(conn, mix, "薛珍珠", "Tiết Trân Châu", note="tên nhân vật chính")
    outcome = await _save(GlossaryStore(info), "薛珍珠", "Xue Zhenzhu", collection_id=mix)
    assert (outcome.status, outcome.existing) == ("exists", "Tiết Trân Châu")
    assert conn.execute(
        "SELECT term_vi, note, workflow_id, source_aweme_id FROM localization.glossary"
    ).fetchall() == [("Tiết Trân Châu", "tên nhân vật chính", "dashboard", "dashboard")]


async def test_terms_a_person_set_come_first_when_the_glossary_is_long(glossary_db) -> None:
    info, conn = glossary_db
    mix = _mix(conn, "mix_a")
    for i in range(LOOKUP_LIMIT):
        conn.execute(
            "INSERT INTO localization.glossary (collection_id, term_zh, term_vi, "
            "source_aweme_id, workflow_id, created_at) "
            "VALUES (%s, %s, %s, '1', 'video-1-g1', now() - interval '1 day')",
            (mix, f"词{i:03d}", f"từ {i}"),
        )
    _dashboard_term(conn, mix, "薛珍珠", "Tiết Trân Châu")
    found = await _lookup(GlossaryStore(info), None, collection_id=mix)
    assert len(found) == LOOKUP_LIMIT
    assert found[0].term_zh == "薛珍珠"


async def test_a_stored_term_the_guard_refuses_is_not_shown_when_the_agent_repeats_it(
    glossary_db,
) -> None:
    """The reply to a repeated term shows the stored text to the model; a stored text the
    guard refuses is held back, and the term is still not overwritten."""
    info, conn = glossary_db
    mix = _mix(conn, "mix_a")
    _dashboard_term(conn, mix, "店铺", "cửa hàng", note="ghé shop-xyz.vn")
    outcome = await _save(GlossaryStore(info), "店铺", "tiệm", collection_id=mix)
    assert (outcome.status, outcome.existing) == ("exists", None)
    assert conn.execute("SELECT term_vi, workflow_id FROM localization.glossary").fetchall() == [
        ("cửa hàng", "dashboard")
    ]


async def test_a_stored_term_the_guard_refuses_is_never_read_back(glossary_db) -> None:
    """Terms reach the table by other ways than this store (the dashboard's own copy of
    the guard, a hand-written row); what the guard refuses never reaches the prompt."""
    info, conn = glossary_db
    mix = _mix(conn, "mix_a")
    _dashboard_term(conn, mix, "店铺", "cửa hàng", note="ghé shop-xyz.vn")
    _dashboard_term(conn, mix, "手机", "gọi 0912345678")
    _dashboard_term(conn, mix, "苹果", "Apple")
    store = GlossaryStore(info)
    assert [r.term_zh for r in await _lookup(store, None, collection_id=mix)] == ["苹果"]
    assert await _lookup(store, ["店铺", "手机"], collection_id=mix) == []


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

"""The glossary against a real Postgres: first writer wins, a run's writes are
capped, and a write the guard refuses leaves no row."""

import asyncio

import psycopg
import pytest

from localization.activities.glossary import (
    MAX_WRITES_PER_RUN,
    TERM_MAX_CHARS,
    GlossaryStore,
    check_term,
)


def _rows(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute(
        "SELECT channel_id, term_zh, term_vi, source_aweme_id, workflow_id "
        "FROM localization.glossary ORDER BY term_zh"
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
    assert "Apple" in again.message


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


def test_check_term_accepts_an_ordinary_term() -> None:
    assert check_term("苹果", "Apple", "hãng điện thoại") is None

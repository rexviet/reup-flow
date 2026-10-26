"""The per-channel glossary the Localizer reads and, within limits, extends.

The agent writes here on the strength of text it read out of a video, and that
text is untrusted. So a write is guarded three ways: it is refused when it looks
like an attempt to plant a link or a number (`check_term`), it can never replace
an established term (`ON CONFLICT DO NOTHING`), and a run can write only so many
terms. A replayed or retried write is therefore harmless: the second attempt finds
the first one's row.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.rows import dict_row

from localization.text.safety import find_contacts, normalise

TERM_MAX_CHARS = 64
NOTE_MAX_CHARS = 200
# Terms one run may add. Counted from the table, by the run's workflow id, so a
# retried activity cannot reset it.
MAX_WRITES_PER_RUN = 20
LOOKUP_LIMIT = 100

_CONTROL_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]")

# One statement: insert unless the run is at its limit; a conflict inserts nothing.
UPSERT_SQL = """
INSERT INTO localization.glossary
  (channel_id, term_zh, term_vi, note, source_aweme_id, workflow_id)
SELECT %(channel_id)s, %(term_zh)s, %(term_vi)s, %(note)s, %(aweme_id)s, %(workflow_id)s
 WHERE (SELECT count(*) FROM localization.glossary WHERE workflow_id = %(workflow_id)s) < %(cap)s
ON CONFLICT (channel_id, term_zh) DO NOTHING
RETURNING term_vi
"""
EXISTING_SQL = """
SELECT term_vi FROM localization.glossary WHERE channel_id = %s AND term_zh = %s
"""
LOOKUP_ALL_SQL = """
SELECT term_zh, term_vi, note FROM localization.glossary
 WHERE channel_id = %s ORDER BY created_at, term_zh LIMIT %s
"""
LOOKUP_TERMS_SQL = """
SELECT term_zh, term_vi, note FROM localization.glossary
 WHERE channel_id = %s AND term_zh = ANY(%s) ORDER BY term_zh
"""


@dataclass(frozen=True)
class GlossaryRow:
    term_zh: str
    term_vi: str
    note: str | None


@dataclass(frozen=True)
class UpsertOutcome:
    status: Literal["inserted", "exists", "rejected", "limit"]
    message: str
    existing: str | None = None


def _digits(text: str) -> set[str]:
    return {ch for ch in normalise(text) if ch.isdecimal()}


def check_term(term_zh: str, term_vi: str, note: str | None) -> str | None:
    """Why this term must not be stored, or None when it may be. The message goes
    back to the agent, so it says what to change."""
    for name, value in (("term_zh", term_zh), ("term_vi", term_vi), ("note", note or "")):
        if _CONTROL_RE.search(value):
            return f"{name} must be a single line of plain text"
    if not term_zh.strip():
        return "term_zh is empty"
    if not term_vi.strip():
        return "term_vi is empty"
    if len(term_zh) > TERM_MAX_CHARS:
        return f"term_zh is longer than {TERM_MAX_CHARS} characters"
    if len(term_vi) > TERM_MAX_CHARS:
        return f"term_vi is longer than {TERM_MAX_CHARS} characters"
    if note is not None and len(note) > NOTE_MAX_CHARS:
        return f"note is longer than {NOTE_MAX_CHARS} characters"
    for name, value in (("term_zh", term_zh), ("term_vi", term_vi), ("note", note or "")):
        if find_contacts(value):
            return f"{name} contains a URL, domain name, e-mail address or phone number"
    extra = _digits(term_vi) - _digits(term_zh)
    if extra:
        return "term_vi contains digits that term_zh does not: " + "".join(sorted(extra))
    return None


class GlossaryStore:
    """Reads and writes `localization.glossary` as the `localization` role. A
    connection per call: a tool call is rare and short."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def lookup(self, channel_id: str, terms: list[str] | None) -> list[GlossaryRow]:
        async with await psycopg.AsyncConnection.connect(
            self._conninfo, autocommit=True, row_factory=dict_row
        ) as conn:
            if terms:
                cursor = await conn.execute(LOOKUP_TERMS_SQL, (channel_id, terms[:LOOKUP_LIMIT]))
            else:
                cursor = await conn.execute(LOOKUP_ALL_SQL, (channel_id, LOOKUP_LIMIT))
            return [GlossaryRow(r["term_zh"], r["term_vi"], r["note"]) async for r in cursor]

    async def upsert(
        self,
        *,
        channel_id: str,
        term_zh: str,
        term_vi: str,
        note: str | None,
        aweme_id: str,
        workflow_id: str,
    ) -> UpsertOutcome:
        term_zh = unicodedata.normalize("NFC", term_zh.strip())
        term_vi = unicodedata.normalize("NFC", term_vi.strip())
        note = note.strip() if note and note.strip() else None
        reason = check_term(term_zh, term_vi, note)
        if reason is not None:
            return UpsertOutcome("rejected", f"not saved: {reason}")
        params = {
            "channel_id": channel_id,
            "term_zh": term_zh,
            "term_vi": term_vi,
            "note": note,
            "aweme_id": aweme_id,
            "workflow_id": workflow_id,
            "cap": MAX_WRITES_PER_RUN,
        }
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            inserted = await (await conn.execute(UPSERT_SQL, params)).fetchone()
            if inserted is not None:
                return UpsertOutcome("inserted", "saved")
            existing = await (await conn.execute(EXISTING_SQL, (channel_id, term_zh))).fetchone()
        if existing is not None:
            return UpsertOutcome(
                "exists", "not changed: the channel already fixes this term", existing[0]
            )
        return UpsertOutcome(
            "limit", f"not saved: this run already added {MAX_WRITES_PER_RUN} terms"
        )

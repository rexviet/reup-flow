"""The glossary the Localizer reads and, within limits, extends.

Terms are stored per collection, and a run reads and writes the glossary of the
collection its input names. An input written before collections existed (a run
started before the migration, or "Go on" on one) names only its channel; its scope is
that channel's channel collection, looked up here in the activity, so a replayed
workflow is not affected. A collection that no longer exists, or a channel without
one, stores nothing: the foreign key refuses the row, and the agent is told so.

The agent writes here on the strength of text it read out of a video, and that
text is untrusted. So a write is guarded three ways: it is refused when it looks
like an attempt to plant a link or a number (`check_term`), it can never replace
an established term (`ON CONFLICT DO NOTHING`, so a term a person set on the
dashboard always wins), and a run can write only so many terms. A replayed or
retried write is therefore harmless: the second attempt finds the first one's row.

Terms also reach the table by other ways (the dashboard's own copy of the guard, a
hand-written row), so a row read back is checked again and dropped when the guard
refuses it: nothing it refuses reaches the prompt.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.errors import ForeignKeyViolation, NotNullViolation
from psycopg.rows import dict_row

from localization.text.safety import find_contacts, normalise

TERM_MAX_CHARS = 64
NOTE_MAX_CHARS = 200
# Terms one run may add. Counted from the table, by the run's workflow id, so a
# retried activity cannot reset it.
MAX_WRITES_PER_RUN = 20
LOOKUP_LIMIT = 100

_CONTROL_RE = re.compile(r"[\r\n\t\x00-\x1f\x7f]")

# The run's collection, or for an input that names none, its channel's collection.
SCOPE = (
    "COALESCE(%(collection_id)s::uuid, (SELECT id FROM public.collections"
    " WHERE kind = 'channel' AND external_id = %(channel_id)s))"
)
# Terms a person set on the dashboard are written with this workflow id.
DASHBOARD = "dashboard"
# One statement: insert unless the run is at its limit; a conflict inserts nothing.
UPSERT_SQL = f"""
INSERT INTO localization.glossary
  (collection_id, term_zh, term_vi, note, source_aweme_id, workflow_id)
SELECT {SCOPE}, %(term_zh)s, %(term_vi)s, %(note)s, %(aweme_id)s, %(workflow_id)s
 WHERE (SELECT count(*) FROM localization.glossary WHERE workflow_id = %(workflow_id)s) < %(cap)s
ON CONFLICT (collection_id, term_zh) DO NOTHING
RETURNING term_vi
"""
EXISTING_SQL = f"""
SELECT term_zh, term_vi, note FROM localization.glossary
 WHERE collection_id = {SCOPE} AND term_zh = %(term_zh)s
"""
# The terms a person set first: within the limit they are the ones that matter most.
LOOKUP_ALL_SQL = f"""
SELECT term_zh, term_vi, note FROM localization.glossary
 WHERE collection_id = {SCOPE}
 ORDER BY workflow_id = '{DASHBOARD}' DESC, created_at, term_zh LIMIT %(limit)s
"""
LOOKUP_TERMS_SQL = f"""
SELECT term_zh, term_vi, note FROM localization.glossary
 WHERE collection_id = {SCOPE} AND term_zh = ANY(%(terms)s) ORDER BY term_zh
"""
MISSING_COLLECTION = "not saved: this video's collection no longer exists"
CHANNEL_WITHOUT_COLLECTION = "not saved: this video's channel has no collection"

log = logging.getLogger(__name__)


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
    connection per call: a tool call is rare and short. `collection_id` is the run's
    collection; None for an input written before collections, which `channel_id`
    then stands in for."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def lookup(
        self, *, collection_id: str | None, channel_id: str, terms: list[str] | None
    ) -> list[GlossaryRow]:
        scope = {"collection_id": collection_id, "channel_id": channel_id}
        async with await psycopg.AsyncConnection.connect(
            self._conninfo, autocommit=True, row_factory=dict_row
        ) as conn:
            if terms:
                cursor = await conn.execute(
                    LOOKUP_TERMS_SQL, {**scope, "terms": terms[:LOOKUP_LIMIT]}
                )
            else:
                cursor = await conn.execute(LOOKUP_ALL_SQL, {**scope, "limit": LOOKUP_LIMIT})
            rows = [GlossaryRow(r["term_zh"], r["term_vi"], r["note"]) async for r in cursor]
        return [r for r in rows if check_term(r.term_zh, r.term_vi, r.note) is None]

    async def upsert(
        self,
        *,
        collection_id: str | None,
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
        scope = {"collection_id": collection_id, "channel_id": channel_id}
        params = {
            **scope,
            "term_zh": term_zh,
            "term_vi": term_vi,
            "note": note,
            "aweme_id": aweme_id,
            "workflow_id": workflow_id,
            "cap": MAX_WRITES_PER_RUN,
        }
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            try:
                inserted = await (await conn.execute(UPSERT_SQL, params)).fetchone()
            except (ForeignKeyViolation, NotNullViolation) as exc:
                # The collection was deleted after the run started (the key refuses
                # it), or an old input's channel has no channel collection (the scope
                # is NULL). A retry cannot change either; the run goes on without it.
                message = (
                    MISSING_COLLECTION
                    if isinstance(exc, ForeignKeyViolation)
                    else CHANNEL_WITHOUT_COLLECTION
                )
                log.warning(
                    "glossary term not stored (collection %s, channel %s): %s",
                    collection_id,
                    channel_id,
                    message,
                )
                return UpsertOutcome("rejected", message)
            if inserted is not None:
                return UpsertOutcome("inserted", "saved")
            existing = await (
                await conn.execute(EXISTING_SQL, {**scope, "term_zh": term_zh})
            ).fetchone()
        if existing is not None:
            # The stored text is shown to the model; one the guard refuses is held back.
            stored_zh, stored_vi, stored_note = existing
            shown = stored_vi if check_term(stored_zh, stored_vi, stored_note) is None else None
            return UpsertOutcome(
                "exists", "not changed: the collection already fixes this term", shown
            )
        return UpsertOutcome(
            "limit", f"not saved: this run already added {MAX_WRITES_PER_RUN} terms"
        )

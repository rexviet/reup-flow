"""Read-only pass over projects awaiting review: cue and mismatch counts from HEAD, and
whether the local Studio folder holds unsaved edits. Never materializes anything."""

import logging

from localization.review_bridge.models import mismatch

log = logging.getLogger("review_worker.scan")

PENDING_SQL = """
SELECT aweme_id, generation
  FROM localization.runs
 WHERE status IN ('ready_for_review', 'needs_human')
   AND project_key = 'review/' || aweme_id || '/g' || generation::text || '/head.json'
 ORDER BY aweme_id, generation
"""

UPSERT_SQL = """
INSERT INTO localization.review_status
  (aweme_id, generation, revision, cue_count, mismatch_count, dirty, checked_at)
VALUES (%(aweme)s, %(gen)s, %(revision)s, %(cues)s, %(mismatch)s, %(dirty)s, now())
ON CONFLICT (aweme_id, generation) DO UPDATE
   SET revision = EXCLUDED.revision, cue_count = EXCLUDED.cue_count,
       mismatch_count = EXCLUDED.mismatch_count, dirty = EXCLUDED.dirty,
       checked_at = now()
"""


def scan_one(repo, aweme: str, generation: int) -> dict:
    _, project, _ = repo.current(aweme, generation)
    return {
        "aweme": aweme,
        "gen": generation,
        "revision": project.revision_id,
        "cues": len(project.cues),
        "mismatch": sum(1 for cue in project.cues if mismatch(cue)),
        "dirty": repo.local_state(aweme, generation, project),
    }


def scan(conn, repo) -> int:
    """Returns how many projects were recorded; one broken project never stops the rest."""
    recorded = 0
    for row in conn.execute(PENDING_SQL).fetchall():
        try:
            conn.execute(UPSERT_SQL, scan_one(repo, row["aweme_id"], row["generation"]))
            recorded += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "scan of %s/g%s failed: %s", row["aweme_id"], row["generation"], type(exc).__name__
            )
    return recorded

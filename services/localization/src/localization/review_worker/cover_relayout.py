"""Give the videos still awaiting review the cover box their collection sets now.

A collection's cover geometry is captured when its run is dispatched, so editing the
collection afterwards would leave its waiting videos with the old box. On every scan this
pass compares each waiting project's HEAD with the collection's current values and, where
they differ, commits a new revision from HEAD: the same cues, the same Studio visual edits,
the new box. A video someone decided on (approved or returned) or already rendered is never
touched, nor one whose local Studio folder holds unsaved edits; a HEAD that moved meanwhile
(a Studio save, a revoice) wins, and the video is looked at again on the next scan.
"""

import logging

from localization.activities.cover_geometry import CoverGeometry
from localization.review_bridge.repository import Conflict
from localization.review_bridge.visual import capture_visual

log = logging.getLogger("review_worker.cover_relayout")

OPERATION = "cover-relayout"

# The `localization` role reads `videos.aweme_id`/`collection_id`, `collections`,
# `review_decisions` and its own `final_renders`.
AWAITING_SQL = """
SELECT r.aweme_id, r.generation,
       c.cover_offset_pct, c.cover_width_pct, c.cover_height_pct
  FROM localization.runs r
  JOIN videos v ON v.aweme_id = r.aweme_id
  JOIN collections c ON c.id = v.collection_id
 WHERE r.status IN ('ready_for_review', 'needs_human')
   AND r.project_key = 'review/' || r.aweme_id || '/g' || r.generation::text || '/head.json'
   AND NOT EXISTS (SELECT FROM review_decisions d
                    WHERE d.aweme_id = r.aweme_id AND d.generation = r.generation)
   AND NOT EXISTS (SELECT FROM localization.final_renders f
                    WHERE f.aweme_id = r.aweme_id AND f.generation = r.generation)
 ORDER BY r.aweme_id, r.generation
"""


def relayout_one(repo, aweme: str, generation: int, geometry: CoverGeometry) -> str | None:
    """The new HEAD revision, or None when HEAD already has `geometry`, the Studio folder
    holds unsaved edits, or HEAD moved before the new revision could take it."""
    _, project, etag = repo.current(aweme, generation)
    before = CoverGeometry.of(project)
    if before == geometry or repo.local_state(aweme, generation, project):
        return None
    candidate = repo.prepare(
        project,
        project.cues,
        capture_visual(repo.read_asset(project.html), project.cues),
        operation=OPERATION,
        geometry=geometry,
    )
    try:
        repo.activate(candidate, etag)
    except Conflict:
        return None
    log.info(
        "cover relayout %s/g%s: %s -> %s (offset %s, width %s, height %s)",
        aweme, generation, project.revision_id, candidate.revision_id,
        geometry.offset_pct, geometry.width_pct, geometry.height_pct,
    )  # fmt: skip
    return candidate.revision_id


def relayout(conn, repo) -> int:
    """Returns how many projects got a new revision; one broken project never stops the
    rest, and a failed read of the queue never stops the scan that follows."""
    try:
        rows = conn.execute(AWAITING_SQL).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("cover relayout skipped: %s", type(exc).__name__)
        return 0
    made = 0
    for row in rows:
        geometry = CoverGeometry(
            offset_pct=row["cover_offset_pct"],
            width_pct=row["cover_width_pct"],
            height_pct=row["cover_height_pct"],
        )
        try:
            if relayout_one(repo, row["aweme_id"], row["generation"], geometry) is not None:
                made += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cover relayout of %s/g%s failed: %s",
                row["aweme_id"], row["generation"], type(exc).__name__,
            )  # fmt: skip
    return made

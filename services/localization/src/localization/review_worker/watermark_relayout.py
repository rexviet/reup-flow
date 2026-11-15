"""Give the videos already made the blur their collection's watermark images call for now.

A run looks for its collection's images when it is made, so an image added (or removed)
afterwards would leave its videos as they were. Each pass compares a project's images
(`watermark_templates`) with the collection's current ones and, where they differ, finds
the watermarks again on the project's source and commits a new revision: the same cues,
the same Studio visual edits, the new blur. With no image left, the new revision has no
blur and nothing is searched.

- A video awaiting review gets the revision as its HEAD, as a cover relayout does.
- An approved video gets it from the approved revision; in one transaction the approval
  moves to it and the final render is queued again, so the delivered file is replaced.
  Never while that render runs; the next pass tries again.
- A returned video is left alone, and so is one whose Studio folder holds unsaved edits
  or whose HEAD moved meanwhile (the next pass looks again).

The search takes minutes, so a pass searches one video at most.
"""

import logging
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from localization.activities.detect_watermarks import find_on_video
from localization.contracts import Watermark, WatermarkTemplate
from localization.review_bridge.repository import Conflict
from localization.review_bridge.visual import capture_visual

log = logging.getLogger("review_worker.watermark_relayout")

OPERATION = "watermark-relayout"

# Projects awaiting review (no decision, no render) and approved ones, with the images
# their collection holds now, in the order the starter snapshots them.
PROJECTS_SQL = """
SELECT r.aweme_id, r.generation, d.approved_revision, f.state AS render_state,
       COALESCE((SELECT json_agg(json_build_object(
                          'key', w.image_key, 'sha256', w.image_sha256,
                          'frame_height', w.frame_height) ORDER BY w.created_at, w.id)
                   FROM collection_watermarks w
                  WHERE w.collection_id = v.collection_id), '[]'::json) AS watermarks
  FROM localization.runs r
  JOIN videos v ON v.aweme_id = r.aweme_id
  LEFT JOIN review_decisions d ON d.aweme_id = r.aweme_id AND d.generation = r.generation
  LEFT JOIN localization.final_renders f
         ON f.aweme_id = r.aweme_id AND f.generation = r.generation
 WHERE r.project_key = 'review/' || r.aweme_id || '/g' || r.generation::text || '/head.json'
   AND ((r.status IN ('ready_for_review', 'needs_human')
         AND d.aweme_id IS NULL AND f.aweme_id IS NULL)
        OR (d.decision = 'approved' AND d.approved_revision IS NOT NULL))
 ORDER BY r.aweme_id, r.generation
"""

# The run row serializes this with the dashboard's approve and return.
LOCK_SQL = """
SELECT 1 FROM localization.runs WHERE aweme_id = %(aweme)s AND generation = %(gen)s FOR UPDATE
"""

STILL_APPROVED_SQL = """
SELECT 1 FROM review_decisions d
 WHERE d.aweme_id = %(aweme)s AND d.generation = %(gen)s
   AND d.decision = 'approved' AND d.approved_revision = %(old)s
   AND NOT EXISTS (SELECT FROM localization.final_renders f
                    WHERE f.aweme_id = d.aweme_id AND f.generation = d.generation
                      AND f.state = 'rendering')
"""

MOVE_APPROVAL_SQL = """
UPDATE review_decisions SET approved_revision = %(new)s
 WHERE aweme_id = %(aweme)s AND generation = %(gen)s
   AND decision = 'approved' AND approved_revision = %(old)s
"""

REQUEUE_SQL = """
INSERT INTO localization.final_renders (aweme_id, generation, approved_revision, state)
VALUES (%(aweme)s, %(gen)s, %(new)s, 'queued')
ON CONFLICT (aweme_id, generation) DO UPDATE
   SET approved_revision = EXCLUDED.approved_revision, state = 'queued', attempts = 0,
       error = NULL, claim = NULL, lease_expires_at = NULL
 WHERE localization.final_renders.state <> 'rendering'
"""

Finder = Callable[[object, tuple[WatermarkTemplate, ...], threading.Event], tuple[Watermark, ...]]


class _Abandoned(Exception):
    """The approval or the render changed under the pass; the next pass looks again."""


class WatermarkRelayout:
    def __init__(self, repo, store, work_root: Path, *, finder: Finder | None = None) -> None:
        self.repo = repo
        self.store = store
        self.work_root = work_root
        self.finder = finder or self._find
        # Projects found in line with their collection's images (and approval), so that
        # the next pass does not read their manifest again until one of those changes.
        self.settled: dict[tuple[str, int], tuple] = {}

    def _find(self, project, templates, cancel) -> tuple[Watermark, ...]:
        self.work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="marks-", dir=self.work_root) as tmp:
            video = Path(tmp) / "source.mp4"
            self.store.download_file(project.video.key, video, produced_by="review_project")
            return find_on_video(self.store, video, templates, project.source_region, cancel)

    def run_pass(self, conn, stopping: threading.Event) -> int:
        """Returns how many projects got a new revision; one broken project never stops
        the rest."""
        made = 0
        for row in conn.execute(PROJECTS_SQL).fetchall():
            if stopping.is_set():
                break
            key = (row["aweme_id"], row["generation"])
            templates = tuple(WatermarkTemplate.model_validate(w) for w in row["watermarks"])
            state = (tuple(t.sha256 for t in templates), row["approved_revision"])
            if self.settled.get(key) == state:
                continue
            try:
                outcome = self._one(conn, row, templates, stopping)
            except Exception as exc:  # noqa: BLE001
                log.warning("watermark relayout of %s/g%s failed: %s", *key, type(exc).__name__)
                continue
            if outcome in ("settled", "made", "moved"):
                self.settled[key] = state
            made += outcome in ("made", "moved")
            if templates and outcome in ("made", "lost"):
                break  # one search a pass
        return made

    def _one(self, conn, row, templates, stopping) -> str:
        """`settled` (nothing to do), `made` (a new revision), `moved` (an approval moved
        to a revision made before), `busy` (not now, nothing searched) or `lost` (HEAD or
        the approval moved while it was searched)."""
        aweme, generation = row["aweme_id"], row["generation"]
        shas = tuple(t.sha256 for t in templates)
        approved = row["approved_revision"]
        _, head, etag = self.repo.current(aweme, generation)
        if approved is None:
            base = head
        elif head.revision_id == approved:
            base = head
        elif (
            head.parent_revision_id == approved
            and head.operation_id == OPERATION
            and head.watermark_templates == shas
        ):
            # HEAD was made by a pass whose approval move did not commit: move it now.
            self._move_approval(conn, aweme, generation, approved, head, None)
            return "moved"
        else:
            return "settled"  # edited after the approval: the dashboard asks for a new one
        if base.watermark_templates == shas:
            return "settled"
        if approved is not None and row["render_state"] == "rendering":
            return "busy"
        if self.repo.local_state(aweme, generation, base):
            return "busy"
        found = self.finder(base, templates, stopping) if templates else ()
        if stopping.is_set():
            return "busy"  # a cancelled search found nothing; it is not an answer
        candidate = self.repo.prepare(
            base,
            base.cues,
            capture_visual(self.repo.read_asset(base.html), base.cues),
            operation=OPERATION,
            watermarks=found,
            watermark_templates=shas,
        )
        try:
            if approved is None:
                self.repo.activate(candidate, etag)
            else:
                self._move_approval(conn, aweme, generation, approved, candidate, etag)
        except (Conflict, _Abandoned):
            return "lost"
        log.info(
            "watermark relayout %s/g%s%s: %s -> %s (%s watermark(s), %s image(s))",
            aweme, generation, " (approved)" if approved else "", base.revision_id,
            candidate.revision_id, len(found), len(templates),
        )  # fmt: skip
        return "made"

    def _move_approval(self, conn, aweme, generation, old, candidate, etag) -> None:
        """HEAD, the approval and the final render move to `candidate` together; `etag`
        None when HEAD already is `candidate`."""
        ids = {"aweme": aweme, "gen": generation, "old": old, "new": candidate.revision_id}
        with conn.transaction():
            conn.execute(LOCK_SQL, ids)
            if conn.execute(STILL_APPROVED_SQL, ids).fetchone() is None:
                raise _Abandoned
            if etag is not None:
                self.repo.activate(candidate, etag)
            if conn.execute(MOVE_APPROVAL_SQL, ids).rowcount != 1:
                raise _Abandoned
            if conn.execute(REQUEUE_SQL, ids).rowcount != 1:
                raise _Abandoned
        log.info("approval of %s/g%s moved %s -> %s; final render queued again",
                 aweme, generation, old, candidate.revision_id)  # fmt: skip

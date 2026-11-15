-- Rollback for 261114-collection-cover-geometry.sql.
--
-- Data loss: every collection's cover position, width and height. Revisions
-- already made with them keep their box.

BEGIN;

ALTER TABLE collections
  DROP COLUMN cover_height_pct,
  DROP COLUMN cover_width_pct,
  DROP COLUMN cover_offset_pct;

COMMIT;

-- Rollback for 261122-video-localize-mode.sql.
--
-- Data loss: which videos were queued for subtitles only. A run already started keeps the
-- mode it was started with; a video localized again afterwards is dubbed in full.

BEGIN;

ALTER TABLE videos DROP COLUMN localize_mode;

COMMIT;

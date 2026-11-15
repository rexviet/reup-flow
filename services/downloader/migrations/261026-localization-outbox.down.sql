-- Rollback for 261026-localization-outbox.sql.
--
-- Deploy downloader code that no longer writes the outbox BEFORE running this:
-- with the table gone, every markDone would fail and burn the video's attempts.
-- No CASCADE, so an unexpected dependent object fails this loudly.

BEGIN;

DROP TABLE localization_outbox;

COMMIT;

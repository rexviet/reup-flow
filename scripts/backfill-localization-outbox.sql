-- Backfill localization_outbox for videos that finished downloading before the
-- outbox existed.
--
-- Preconditions:
--   * The downloader code that writes the outbox in markDone is already
--     deployed, otherwise videos finishing during the backfill get no row.
--   * pg_dump the database first.
--   * Run only after the object-storage migration has completed, so every
--     candidate already has an s3:// storage_path. Local-path videos are
--     skipped on purpose: the localization side cannot read them.
--
-- Every inserted row is 'held': nothing is dispatched until an admin moves it
-- to 'pending' (dashboard localize), so a large backlog cannot flood the
-- dispatcher.
--
-- Idempotent: rows that already exist are left alone.
--
--   psql "$DATABASE_URL" -f scripts/backfill-localization-outbox.sql

BEGIN;

INSERT INTO localization_outbox (aweme_id, state)
SELECT aweme_id, 'held'
  FROM videos
 WHERE status = 'done'
   AND storage_path LIKE 's3://%'
ON CONFLICT DO NOTHING;

COMMIT;

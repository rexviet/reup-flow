-- Backfill localization_outbox for videos that finished downloading before the
-- outbox existed.
--
-- Preconditions:
--   * pg_dump the database first.
--   * Run only after the object-storage migration has completed, so every
--     candidate already has an s3:// storage_path. Local-path videos are
--     skipped on purpose: the localization side cannot read them.
--
-- Idempotent: rows that already exist are left alone.
--
--   psql "$DATABASE_URL" -f scripts/backfill-localization-outbox.sql

BEGIN;

INSERT INTO localization_outbox (aweme_id)
SELECT aweme_id
  FROM videos
 WHERE status = 'done'
   AND storage_path LIKE 's3://%'
ON CONFLICT DO NOTHING;

COMMIT;

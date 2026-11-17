-- Rollback for 261115-collection-watermark-uploads.sql.
--
-- Refuses while an uploaded image (one with no cut position) exists: delete those
-- images on the dashboard first. Cut images are kept.

BEGIN;

DO $$ BEGIN
  IF EXISTS (SELECT FROM collection_watermarks WHERE aweme_id IS NULL) THEN
    RAISE EXCEPTION 'uploaded watermark images exist; delete them before rolling back';
  END IF;
END $$;

ALTER TABLE collection_watermarks
  DROP CONSTRAINT collection_watermarks_region_check,
  ALTER COLUMN aweme_id SET NOT NULL,
  ALTER COLUMN time_ms SET NOT NULL,
  ALTER COLUMN x SET NOT NULL,
  ALTER COLUMN y SET NOT NULL,
  ADD CONSTRAINT collection_watermarks_region_check CHECK (x + w <= frame_width AND y + h <= frame_height);

COMMIT;

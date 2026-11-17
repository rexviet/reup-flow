-- A watermark image can also be a file uploaded from the admin's machine instead of a
-- region cut on the dashboard. Such an image has no video, moment or position it was cut
-- at, so those four columns become optional: all four are set (a cut) or none (an upload).
-- The image still says the size of the frame it belongs to (its base scale) and must fit
-- in it.

BEGIN;

ALTER TABLE collection_watermarks
  ALTER COLUMN aweme_id DROP NOT NULL,
  ALTER COLUMN time_ms DROP NOT NULL,
  ALTER COLUMN x DROP NOT NULL,
  ALTER COLUMN y DROP NOT NULL,
  DROP CONSTRAINT collection_watermarks_region_check,
  ADD CONSTRAINT collection_watermarks_region_check CHECK (
    w <= frame_width AND h <= frame_height
    AND num_nulls(aweme_id, time_ms, x, y) IN (0, 4)
    AND (x IS NULL OR (x + w <= frame_width AND y + h <= frame_height))
  );

COMMIT;

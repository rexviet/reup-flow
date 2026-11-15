-- Watermark images of a collection: each one a region a person cut from a frame of one
-- of its videos on the dashboard. A run of the collection finds each image on the source
-- by template matching and blurs it where and while it shows; a collection with none is
-- left alone. The PNG lives in object storage under the downloader's own prefix
-- (`source/collections/<collection>/watermarks/<id>.png`), which the localization user
-- already reads; `image_key` is that full object key.
--
-- The localization worker also moves an approval onto a new revision when it adds the
-- blur to a video already approved, so it gets UPDATE on that one column of
-- review_decisions (it already reads the table).

BEGIN;

CREATE TABLE collection_watermarks (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  collection_id uuid        NOT NULL REFERENCES collections (id) ON DELETE CASCADE,
  image_key     text        NOT NULL UNIQUE,
  image_sha256  text        NOT NULL CHECK (image_sha256 ~ '^[0-9a-f]{64}$'),
  -- The frame the region was cut from: its size is the image's base scale.
  frame_width   integer     NOT NULL CHECK (frame_width > 0),
  frame_height  integer     NOT NULL CHECK (frame_height > 0),
  -- Where it was cut, for display and audit.
  aweme_id      text        NOT NULL,
  time_ms       integer     NOT NULL CHECK (time_ms >= 0),
  x             integer     NOT NULL CHECK (x >= 0),
  y             integer     NOT NULL CHECK (y >= 0),
  w             integer     NOT NULL CHECK (w >= 8),
  h             integer     NOT NULL CHECK (h >= 8),
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT collection_watermarks_region_check CHECK (x + w <= frame_width AND y + h <= frame_height)
);

CREATE INDEX collection_watermarks_collection_idx ON collection_watermarks (collection_id, created_at, id);

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'localization') THEN
    GRANT SELECT ON collection_watermarks TO localization;
    GRANT UPDATE (approved_revision) ON review_decisions TO localization;
  END IF;
END $$;

COMMIT;

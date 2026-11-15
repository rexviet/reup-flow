-- Records which videos scripts/migrate-local-storage-to-s3.ts moved from the
-- local disk to S3, and where they were before, so `--revert` can put exactly
-- those rows back and nothing else. Rollback is
-- migrations/261026-storage-migration-map.down.sql.
--
-- Table name is UNQUALIFIED on purpose, like every migration here: it lands
-- wherever `search_path` points (`public` in production, a per-run schema under
-- jest).
--
-- No foreign key to videos: the map is the evidence of a one-off data move and
-- must outlive any decision to delete the video row.

BEGIN;

CREATE TABLE storage_migration_map (
  aweme_id     text        PRIMARY KEY,
  old_path     text        NOT NULL,
  new_path     text        NOT NULL,
  migrated_at  timestamptz NOT NULL DEFAULT now()
);

COMMIT;

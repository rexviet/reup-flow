-- Rollback for 261115-collection-watermarks.sql.
--
-- Data loss: every collection's watermark images (the rows; the PNG objects stay in
-- storage). Review revisions already made with a blur keep it.

BEGIN;

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'localization') THEN
    REVOKE UPDATE (approved_revision) ON review_decisions FROM localization;
  END IF;
END $$;

DROP TABLE collection_watermarks;

COMMIT;

-- Rollback for 261026-storage-migration-map.sql.
--
-- Dropping the map loses the only record of which rows were moved to S3. Run
-- `scripts/migrate-local-storage-to-s3.ts --revert` FIRST if any video still
-- points at s3://; afterwards the map is empty and this drop loses nothing.

BEGIN;

DROP TABLE IF EXISTS storage_migration_map;

COMMIT;

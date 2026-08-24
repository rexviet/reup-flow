-- Rollback for 260824-init-schema.sql.
--
-- Greenfield migration, so the rollback is a plain drop -- there is no data
-- to preserve. Order matters only through jobs -> profiles; CASCADE is
-- deliberately NOT used, so an unexpected dependent object (a view someone
-- added, another table's FK) makes this fail loudly instead of silently
-- taking that object down with it.
--
-- Indexes are dropped implicitly with their tables; listing them separately
-- would only create a second place to forget one.

BEGIN;

DROP TABLE IF EXISTS videos;
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS profiles;

COMMIT;

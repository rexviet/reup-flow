-- Rollback for 261026-localization-core.sql. Drops every table in the schema and
-- the data in them; take a backup first if any of it matters.
DROP SCHEMA localization CASCADE;

-- Rollback for 261026-localization-core.sql. Drops exactly the three tables the
-- migration created, then the schema WITHOUT CASCADE: if anything else has been
-- put in the schema since, the final statement fails and nothing it does not own
-- is destroyed. The three tables hold data; take a backup first if it matters.
DROP TABLE localization.runs;
DROP TABLE localization.voice_rates;
DROP TABLE localization.glossary;
DROP SCHEMA localization;

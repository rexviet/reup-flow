-- Rollback for 261115-outbox-bypass-skips.sql.
--
-- Data loss: which runs were let past their skip checks. A run already gone on from keeps
-- what it made; one still pending with the flag is checked again as usual.

BEGIN;

ALTER TABLE localization_outbox DROP COLUMN bypass_skips;

COMMIT;

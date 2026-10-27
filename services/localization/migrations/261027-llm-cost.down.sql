-- Rollback for 261027-llm-cost.sql. No CASCADE: an unexpected dependent object makes
-- this fail loudly instead of being dropped with it.

BEGIN;

ALTER TABLE localization.runs DROP COLUMN llm_cost_usd;
DROP TABLE localization.llm_calls;

COMMIT;

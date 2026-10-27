-- Rollback for 261028-llm-call-provider.sql. Stop the agents worker first: its recorder
-- names the column.

BEGIN;

ALTER TABLE localization.llm_calls DROP COLUMN provider;

COMMIT;

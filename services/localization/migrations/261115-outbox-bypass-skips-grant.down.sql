-- Rollback for 261115-outbox-bypass-skips-grant.sql.

BEGIN;

REVOKE SELECT (bypass_skips) ON localization_outbox FROM localization;

COMMIT;

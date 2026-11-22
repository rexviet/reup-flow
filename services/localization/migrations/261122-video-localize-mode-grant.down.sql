-- Rollback for 261122-video-localize-mode-grant.sql.

BEGIN;

REVOKE SELECT (localize_mode) ON videos FROM localization;

COMMIT;

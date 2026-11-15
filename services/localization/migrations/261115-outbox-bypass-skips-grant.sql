-- The starter (claiming a row) and the worker (a skip check deciding whether to let a video
-- through) read whether the operator asked for a run's skip checks to be set aside. Read
-- only: the dashboard alone sets it. Applied after 261115-outbox-bypass-skips.sql.

BEGIN;

GRANT SELECT (bypass_skips) ON localization_outbox TO localization;

COMMIT;

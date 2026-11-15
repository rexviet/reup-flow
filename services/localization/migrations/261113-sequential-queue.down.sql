-- Rollback for 261113-sequential-queue.sql. Held videos stay held; the in-flight record is
-- dropped with its table.

BEGIN;

DROP TRIGGER auto_queue_row_held ON localization_outbox;
DROP TRIGGER auto_queue_dispatch_gave_up ON localization_outbox;
DROP TRIGGER auto_queue_run_written ON localization.runs;
DROP FUNCTION auto_queue_on_held();
DROP FUNCTION auto_queue_on_finish();
DROP FUNCTION auto_queue_release_next();
DROP TABLE auto_queue_inflight;

COMMIT;

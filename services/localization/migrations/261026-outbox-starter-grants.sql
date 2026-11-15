-- What the outbox starter (role `localization`) may do in the application
-- database. The tables are unqualified on purpose, like the migrations that
-- created them: they resolve through search_path (`public` in production).
--
-- Column by column, because the starter is one process with one job. It reads the
-- three columns of a video it needs to dispatch, reads the outbox columns its
-- claim and settle statements look at, and updates only the bookkeeping
-- columns. It cannot change `generation` (that restarts a video: an operator
-- decision), cannot insert or delete rows (the downloader does that), and cannot
-- touch any other table of the downloader.
--
-- Raw SQL, applied by an admin with `psql -v ON_ERROR_STOP=1 -1 -f` after the
-- outbox migration. Idempotent.

GRANT SELECT (aweme_id, sec_user_id, storage_path) ON videos TO localization;
GRANT SELECT (aweme_id, generation, state, next_attempt_at, dispatch_attempts)
  ON localization_outbox TO localization;
GRANT UPDATE (state, next_attempt_at, dispatch_attempts, last_error, dispatched_at, workflow_id)
  ON localization_outbox TO localization;

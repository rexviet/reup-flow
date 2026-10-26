-- What the outbox starter (role `localization`) may do in the application
-- database. The tables are unqualified on purpose, like the migrations that
-- created them: they resolve through search_path (`public` in production).
--
-- It reads the video it dispatches, and settles the outbox row. It cannot
-- insert or delete outbox rows (the downloader does that) and cannot touch any
-- other table of the downloader.
--
-- Raw SQL, applied by an admin with `psql -v ON_ERROR_STOP=1 -1 -f` after the
-- outbox migration. Idempotent.

GRANT SELECT ON videos TO localization;
GRANT SELECT, UPDATE ON localization_outbox TO localization;

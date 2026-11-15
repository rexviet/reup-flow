-- The OpenRouter provider that served each model request (the `provider` of the chat
-- completion body, e.g. "Crusoe", "Relace"). The same model answers in 5 s from one
-- provider and in 40 s from another, so a slow or expensive call cannot be explained
-- without it. NULL for rows written before this column and for a response without one.
--
-- Raw SQL, applied by an admin with `psql -v ON_ERROR_STOP=1 -1 -f` AFTER
-- 261027-llm-cost.sql, and before the agents worker that writes the column starts: the
-- recorder's INSERT names it, and a recorder that cannot write logs a warning and drops
-- the row. Back up first (pg_dump) on a database that holds data; the change only adds a
-- nullable column, so it rewrites nothing.

ALTER TABLE localization.llm_calls ADD COLUMN provider text;

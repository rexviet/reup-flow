-- Finds final renders by state. `aweme_id` rides along so the dashboard's rendered-video
-- list (`WHERE state = 'rendered'`, distinct aweme_id) is answered from the index alone,
-- without reading the table, however many renders accumulate.
--
-- Applied by scripts/apply-migrations.sh, which takes a pg_dump first. Builds without
-- CONCURRENTLY because it runs inside the script's transaction.

BEGIN;

CREATE INDEX final_renders_state_idx ON localization.final_renders (state, aweme_id);

COMMIT;

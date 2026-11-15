-- The real cost of every model request, as OpenRouter reports it, and the total per run.
--
-- `llm_calls` has one row per OpenRouter generation id, so a request that is
-- recorded twice (an activity retried after the response arrived) counts once.
-- `runs.llm_cost_usd` is the sum over a video and generation, set when the run's
-- row is written; NULL when no cost was reported for any of its requests.
--
-- Raw SQL, applied by an admin with `psql -v ON_ERROR_STOP=1 -1 -f` AFTER the core
-- migration, and after a pg_dump on a database that holds data.

CREATE TABLE localization.llm_calls (
  generation_id      text          PRIMARY KEY,
  aweme_id           text          NOT NULL,
  generation         integer       NOT NULL CHECK (generation >= 0),
  stage              text          NOT NULL,
  model              text,
  prompt_tokens      integer       CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
  completion_tokens  integer       CHECK (completion_tokens IS NULL OR completion_tokens >= 0),
  cost_usd           numeric(14,8) CHECK (cost_usd IS NULL OR cost_usd >= 0),
  created_at         timestamptz   NOT NULL DEFAULT now()
);

CREATE INDEX llm_calls_run_idx ON localization.llm_calls (aweme_id, generation);

ALTER TABLE localization.llm_calls OWNER TO localization;

ALTER TABLE localization.runs ADD COLUMN llm_cost_usd numeric(14,8);

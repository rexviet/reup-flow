-- Core tables of the localization service, in their own schema owned by the
-- `localization` role (created by the db-init service).
--
-- Raw SQL, applied by an admin with `psql -v ON_ERROR_STOP=1 -1 -f`. Back the
-- database up (pg_dump) before applying it to a database that holds data.

CREATE SCHEMA localization AUTHORIZATION localization;

-- Terms the Localizer fixed for a channel. First writer wins (ON CONFLICT DO
-- NOTHING), so a replayed activity never changes an established translation.
CREATE TABLE localization.glossary (
  channel_id       text        NOT NULL,
  term_zh          text        NOT NULL,
  term_vi          text        NOT NULL CHECK (char_length(term_vi) BETWEEN 1 AND 64),
  note             text        CHECK (note IS NULL OR char_length(note) <= 200),
  source_aweme_id  text        NOT NULL,
  workflow_id      text        NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (channel_id, term_zh)
);

-- Measured speaking rate per voice, used to estimate how long a line will take.
CREATE TABLE localization.voice_rates (
  voice_hash         text             PRIMARY KEY,
  syllables_per_sec  double precision NOT NULL CHECK (syllables_per_sec > 0),
  measured_at        timestamptz      NOT NULL DEFAULT now()
);

-- One row per localization run; `generation` is bumped to run a video again.
CREATE TABLE localization.runs (
  aweme_id       text        NOT NULL,
  generation     integer     NOT NULL CHECK (generation >= 0),
  workflow_id    text        NOT NULL,
  status         text        NOT NULL,
  output_key     text,
  review_key     text,
  verdict        jsonb,
  review_rounds  integer     NOT NULL DEFAULT 0 CHECK (review_rounds >= 0),
  needs_human    boolean     NOT NULL DEFAULT false,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (aweme_id, generation)
);

ALTER TABLE localization.glossary     OWNER TO localization;
ALTER TABLE localization.voice_rates  OWNER TO localization;
ALTER TABLE localization.runs         OWNER TO localization;

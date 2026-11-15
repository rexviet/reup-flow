-- Review delivery: final renders of approved revisions, review scan results and
-- the heartbeat of the host worker. Owned by the `localization` role like the
-- core tables, so the worker (NOSUPERUSER) can write them directly.
--
-- The downloader inserts a 'queued' final_renders row when an admin approves a
-- revision; the worker writes the rest. The worker renders only when
-- public.review_decisions holds an approval for the same revision, so a row
-- inserted by the pipeline alone never passes the approval gate.

BEGIN;

CREATE TABLE localization.final_renders (
  aweme_id          text        NOT NULL,
  generation        integer     NOT NULL CHECK (generation >= 0),
  approved_revision text        NOT NULL,
  state             text        NOT NULL DEFAULT 'queued'
                    CHECK (state IN ('queued','rendering','rendered','failed')),
  attempts          integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  claim             uuid,
  lease_expires_at  timestamptz,
  error             text,
  final_key         text,
  srt_key           text,
  rendered_at       timestamptz,
  PRIMARY KEY (aweme_id, generation)
);

CREATE INDEX final_renders_queued_idx
  ON localization.final_renders (aweme_id, generation) WHERE state = 'queued';

CREATE TABLE localization.review_status (
  aweme_id        text        NOT NULL,
  generation      integer     NOT NULL CHECK (generation >= 0),
  revision        text,
  cue_count       integer,
  mismatch_count  integer,
  dirty           boolean,
  checked_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (aweme_id, generation)
);

CREATE TABLE localization.host_workers (
  name     text        PRIMARY KEY,
  seen_at  timestamptz NOT NULL DEFAULT now(),
  info     jsonb       NOT NULL DEFAULT '{}'
);

ALTER TABLE localization.final_renders OWNER TO localization;
ALTER TABLE localization.review_status OWNER TO localization;
ALTER TABLE localization.host_workers  OWNER TO localization;

COMMIT;

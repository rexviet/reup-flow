-- Admin dashboard: hold videos for review, per-channel auto-localize switch,
-- approval decisions, publication state, Studio open requests and an action log.
--
-- Table names are unqualified like the earlier downloader migrations: they land
-- wherever search_path points (`public` in production, a per-run schema in
-- tests). Only the downloader writes `review_decisions`; the host worker (role
-- `localization`) may read it and update the bookkeeping columns of
-- `studio_open_requests`, nothing else. Grants are skipped when the role does
-- not exist so a disposable test schema does not need it.

BEGIN;

-- Widen the outbox state check with 'held'. The starter only claims 'pending',
-- so a held row is never dispatched. Look the constraint up instead of guessing.
DO $$
DECLARE
  con text; BEGIN
  FOR con IN
    SELECT c.conname FROM pg_constraint c
     WHERE c.conrelid = to_regclass('localization_outbox')
       AND c.contype = 'c'
       AND pg_get_constraintdef(c.oid) LIKE '%state%'
  LOOP
    EXECUTE format('ALTER TABLE localization_outbox DROP CONSTRAINT %I', con);
  END LOOP;
END $$;

ALTER TABLE localization_outbox
  ADD CONSTRAINT localization_outbox_state_check
  CHECK (state IN ('pending','held','dispatched','rejected','dead'));

ALTER TABLE profiles ADD COLUMN auto_localize boolean NOT NULL DEFAULT false;

-- NULL means follow the channel switch.
ALTER TABLE videos ADD COLUMN localize_on_done boolean;

CREATE TABLE review_decisions (
  aweme_id          text        NOT NULL,
  generation        integer     NOT NULL,
  decision          text        NOT NULL CHECK (decision IN ('approved','returned')),
  approved_revision text,
  confirm_mismatch  boolean     NOT NULL DEFAULT false,
  note              text,
  decided_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (aweme_id, generation)
);

CREATE TABLE publications (
  aweme_id    text        NOT NULL,
  generation  integer     NOT NULL,
  state       text        NOT NULL DEFAULT 'unpublished'
              CHECK (state IN ('unpublished','scheduled','publishing','published','failed')),
  platform    text,
  post_url    text,
  note        text,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (aweme_id, generation)
);

CREATE TABLE studio_open_requests (
  id           bigserial   PRIMARY KEY,
  aweme_id     text        NOT NULL,
  generation   integer     NOT NULL,
  state        text        NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','failed')),
  error        text,
  requested_at timestamptz NOT NULL DEFAULT now(),
  handled_at   timestamptz
);

CREATE TABLE dashboard_actions (
  id          bigserial   PRIMARY KEY,
  aweme_id    text        NOT NULL,
  action      text        NOT NULL,
  detail      jsonb       NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX dashboard_actions_aweme_idx ON dashboard_actions (aweme_id, created_at DESC);

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'localization') THEN
    GRANT SELECT ON review_decisions TO localization;
    GRANT SELECT, UPDATE (state, error, handled_at) ON studio_open_requests TO localization;
  END IF;
END $$;

COMMIT;

-- localization_outbox: one row per finished download that the localization
-- side still has to pick up.
--
-- The invariant: a video that is `done` ALWAYS has a row here. markDone writes
-- both in one transaction, so the downloader never needs to know the consumer
-- (or its workflow engine) exists.
--
-- Table name is deliberately unqualified, like the init migration: it lands
-- wherever search_path points (`public` in production, a per-run schema in
-- tests). Grants to the `localization` role are an ops step, not part of this
-- file, so a test schema does not need that role to exist.

BEGIN;

CREATE TABLE localization_outbox (
  aweme_id          text PRIMARY KEY REFERENCES videos(aweme_id),
  generation        integer NOT NULL DEFAULT 1,
  state             text NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','dispatched','rejected','dead')),
  enqueued_at       timestamptz NOT NULL DEFAULT now(),
  next_attempt_at   timestamptz NOT NULL DEFAULT now(),
  dispatched_at     timestamptz,
  workflow_id       text,
  dispatch_attempts integer NOT NULL DEFAULT 0,
  last_error        text
);

CREATE INDEX localization_outbox_due_idx
  ON localization_outbox (next_attempt_at) WHERE state = 'pending';

COMMIT;

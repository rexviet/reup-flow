-- Phase 1 forward migration -- solution design §6, plan phase-01 §1.1-§1.2.
--
-- Greenfield: these are the first three tables, so no maintenance window and
-- no backfill. Rollback is migrations/260824-init-schema.down.sql.
--
-- Table names are UNQUALIFIED on purpose: they land wherever `search_path`
-- points, which is `public` in production and a per-worker test schema under
-- jest (see test/support/db.ts). Keep it that way.
--
-- This file and services/downloader/src/db/schema.ts describe the same
-- tables. test/db/schema-migration.spec.ts applies THIS file and compares the
-- resulting catalog against the Drizzle definition in both directions, so the
-- two cannot drift apart unnoticed.

BEGIN;

CREATE TABLE profiles (
  sec_user_id     text        PRIMARY KEY,
  nickname        text,
  last_synced_at  timestamptz
);

CREATE TABLE jobs (
  id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sec_user_id       text        NOT NULL REFERENCES profiles (sec_user_id),
  -- pending·enumerating·downloading·completed·failed·blocked·cancelled
  status            text        NOT NULL DEFAULT 'pending',
  -- NULL until enumerate knows the real total.
  total_videos      integer,
  enumerated_count  integer     NOT NULL DEFAULT 0,
  downloaded_count  integer     NOT NULL DEFAULT 0,
  failed_count      integer     NOT NULL DEFAULT 0,
  -- Enumerate checkpoint, written once per page (phase 5).
  max_cursor        bigint      NOT NULL DEFAULT 0,
  -- F5: a crash mid-enumerate would otherwise strand the job in
  -- `enumerating` forever, because the lane only picks up `pending`.
  lease_expires_at  timestamptz,
  -- Operator-facing failure reason. Never contains a cookie (plan §0.3).
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);

-- F7: NO job_id column here, and never add one. `aweme_id` is the global
-- primary key across every job and every run, which is precisely what makes
-- incremental re-runs free. See the long-form explanation on the `videos`
-- table in src/db/schema.ts before changing anything in this block.
CREATE TABLE videos (
  aweme_id          text        PRIMARY KEY,
  sec_user_id       text        NOT NULL,
  -- "desc" is a reserved word; it only parses quoted.
  "desc"            text,
  create_time       timestamptz NOT NULL,
  duration_ms       integer,
  -- F1: cached from the enumerate response, re-resolved REACTIVELY on
  -- expiry/403/410 (phase 6) rather than before every download.
  play_addr         text,
  url_expires_at    timestamptz,
  storage_path      text,
  -- pending·downloading·done·failed·skipped
  status            text        NOT NULL DEFAULT 'pending',
  -- F6: incremented at CLAIM time, not at failure time.
  attempts          integer     NOT NULL DEFAULT 0,
  last_error        text,
  -- F3: without this, a worker that dies mid-download leaves the row in
  -- `downloading` with nobody working on it, forever.
  lease_expires_at  timestamptz
);

-- Claim path. PARTIAL: the index holds only outstanding work, so it shrinks
-- as a job progresses instead of growing with the archive. Dropping the
-- WHERE clause still answers every query correctly -- it just seq-scans
-- 100k rows to find the next 1 -- which is why the predicate is asserted
-- explicitly in schema-migration.spec.ts.
CREATE INDEX videos_pending_claim_idx
  ON videos (status, create_time) WHERE status = 'pending';

-- Reaper path (F3). Partial for the same reason.
CREATE INDEX videos_downloading_lease_idx
  ON videos (status, lease_expires_at) WHERE status = 'downloading';

-- Browse + progress counting. Deliberately NOT partial: it must cover every
-- status, because progress is counted across all of them (F7).
CREATE INDEX videos_profile_recent_idx
  ON videos (sec_user_id, create_time DESC);

-- Claim path for the enumerate lane (FIFO by created_at).
CREATE INDEX jobs_status_created_idx ON jobs (status, created_at);

-- Reaper path for F5.
CREATE INDEX jobs_enumerating_lease_idx
  ON jobs (status, lease_expires_at) WHERE status = 'enumerating';

CREATE INDEX jobs_sec_user_id_idx ON jobs (sec_user_id);

COMMIT;

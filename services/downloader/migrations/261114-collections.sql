-- Collections: every video belongs to exactly one collection (a Douyin 合集, a
-- channel, or one made by hand), which carries its own render settings and the
-- folder its finished videos are saved to on the Mac. Folder exports are queued
-- here by the downloader and carried out by the Mac worker (role
-- `localization`).
--
-- Table names are unqualified like the earlier downloader migrations: they land
-- wherever search_path points (`public` in production, a per-run schema in
-- tests). Grants are skipped when the role does not exist.
--
-- Backfill: each existing profile gets its channel collection and each existing
-- video moves into it. A video whose channel has no profile stops the migration
-- with a count: nothing here can say which collection it belongs to. Apply in
-- the maintenance window with every service stopped; the code that runs after
-- it writes `collection_id` on every new video.

BEGIN;

CREATE TABLE collections (
  id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  kind               text        NOT NULL CHECK (kind IN ('channel','mix','manual')),
  -- `sec_user_id` for a channel, `mix_id` for a 合集, NULL for a manual collection.
  external_id        text,
  -- The channel the collection belongs to; NULL for a manual one made from nothing.
  sec_user_id        text        REFERENCES profiles (sec_user_id),
  name               text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 200),
  -- Background-music offset in dB, in steps of 0.5 so the value is exact in binary.
  bgm_offset_db      real        CHECK (bgm_offset_db BETWEEN -24 AND 24
                                        AND bgm_offset_db * 2 = trunc(bgm_offset_db * 2)),
  caption_text_color text        CHECK (caption_text_color ~ '^#[0-9a-f]{6}$'),
  cover_color        text        CHECK (cover_color ~ '^#[0-9a-f]{6}$'),
  -- Same unit as `Cover.opacity_pct` in the edit spec: a whole percentage.
  cover_opacity_pct  smallint    CHECK (cover_opacity_pct BETWEEN 0 AND 100),
  -- Relative to REVIEW_WORKER_EXPORT_ROOT on the Mac: no leading '/', no empty,
  -- '.' or '..' segment, no control character. (Postgres text cannot hold NUL.)
  save_subdir        text        CHECK (char_length(save_subdir) BETWEEN 1 AND 512
                                        AND save_subdir !~ '(^|/)\.{0,2}(/|$)'
                                        AND save_subdir !~ '[[:cntrl:]]'),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT collections_kind_external_id_key UNIQUE (kind, external_id),
  CONSTRAINT collections_external_id_check CHECK ((external_id IS NULL) = (kind = 'manual')),
  -- A channel collection is found by `external_id` and grouped by `sec_user_id`
  -- (the glossary rollback does), so both name the channel; a 合集 always
  -- belongs to the channel it was seen on.
  CONSTRAINT collections_owner_check CHECK (
    (kind <> 'channel' OR sec_user_id IS NOT DISTINCT FROM external_id) AND (kind <> 'mix' OR sec_user_id IS NOT NULL)
  )
);

-- Nullable until the backfill below has filled it.
ALTER TABLE videos ADD COLUMN collection_id uuid REFERENCES collections (id);
ALTER TABLE videos ADD COLUMN mix_episode integer CHECK (mix_episode > 0);
-- True when a person chose the collection (intake form or a move); enumerate
-- never moves such a video.
ALTER TABLE videos ADD COLUMN collection_manual boolean NOT NULL DEFAULT false;
-- The collection picked by hand for every video the job brings in, if any.
ALTER TABLE jobs ADD COLUMN collection_id uuid REFERENCES collections (id);

DO $$
DECLARE
  orphans bigint; BEGIN
  SELECT count(*) INTO orphans FROM videos v
   WHERE NOT EXISTS (SELECT FROM profiles p WHERE p.sec_user_id = v.sec_user_id);
  IF orphans > 0 THEN
    RAISE EXCEPTION '% videos belong to a channel without a profile; add the profiles or remove the videos, then migrate again', orphans;
  END IF;
END $$;

INSERT INTO collections (kind, external_id, sec_user_id, name)
SELECT 'channel', sec_user_id, sec_user_id,
       coalesce(nullif(btrim(left(nickname, 200)), ''), left(sec_user_id, 200))
  FROM profiles;

UPDATE videos v SET collection_id = c.id
  FROM collections c
 WHERE c.kind = 'channel' AND c.external_id = v.sec_user_id;

ALTER TABLE videos ALTER COLUMN collection_id SET NOT NULL;

-- Browse a collection newest first.
CREATE INDEX videos_collection_recent_idx ON videos (collection_id, create_time DESC);

-- One request to save a finished video into its collection's folder on the Mac.
-- Target and file name are captured when the request is made, so renaming the
-- collection or moving the video later does not redirect a queued export.
CREATE TABLE folder_exports (
  id               bigserial   PRIMARY KEY,
  aweme_id         text        NOT NULL REFERENCES videos (aweme_id),
  generation       integer     NOT NULL,
  collection_id    uuid        NOT NULL REFERENCES collections (id),
  target_subdir    text        NOT NULL CHECK (char_length(target_subdir) BETWEEN 1 AND 512
                                               AND target_subdir !~ '(^|/)\.{0,2}(/|$)'
                                               AND target_subdir !~ '[[:cntrl:]]'),
  file_stem        text        NOT NULL,
  final_key        text        NOT NULL,
  srt_key          text,
  state            text        NOT NULL DEFAULT 'pending'
                               CHECK (state IN ('pending','exporting','done','failed')),
  claim            uuid,
  lease_expires_at timestamptz,
  error            text,
  files            jsonb,
  requested_at     timestamptz NOT NULL DEFAULT now(),
  handled_at       timestamptz,
  -- `E{episode:02d}-{aweme_id}` or `{aweme_id}` of this very video; never text
  -- from Douyin.
  CONSTRAINT folder_exports_file_stem_check CHECK (
    file_stem ~ '^(E[0-9]{2,}-)?[0-9]+$' AND regexp_replace(file_stem, '^E[0-9]+-', '') = aweme_id
  )
);

-- The worker claims open requests in id order.
CREATE INDEX folder_exports_open_idx ON folder_exports (id)
  WHERE state IN ('pending','exporting');
-- At most one open request per video generation: a second click (or a retried
-- request) conflicts instead of exporting the same file twice.
CREATE UNIQUE INDEX folder_exports_one_open_idx ON folder_exports (aweme_id, generation)
  WHERE state IN ('pending','exporting');

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'localization') THEN
    GRANT SELECT (collection_id, mix_episode) ON videos TO localization;
    -- SELECT only: the glossary's foreign key is created by an admin, and the
    -- key check runs as the owner of collections.
    GRANT SELECT ON collections TO localization;
    GRANT SELECT, UPDATE (state, claim, lease_expires_at, error, files, handled_at)
      ON folder_exports TO localization;
  END IF;
END $$;

COMMIT;

-- What a video's localization makes of it, chosen when it is queued (the extension's
-- "subtitles only" switch): `full` dubs it; `subtitles_only` only translates its burned-in
-- subtitles and lays them over the original picture and sound. The starter reads it when
-- it dispatches a run, so changing it later never changes a run already started.

BEGIN;

ALTER TABLE videos
  ADD COLUMN localize_mode text NOT NULL DEFAULT 'full'
  CONSTRAINT videos_localize_mode_check CHECK (localize_mode IN ('full', 'subtitles_only'));

COMMIT;

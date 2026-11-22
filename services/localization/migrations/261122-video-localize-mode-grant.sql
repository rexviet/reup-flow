-- The starter reads what a video's localization makes of it (dubbed in full, or its
-- subtitles only) when it dispatches the run. Read only: the downloader alone sets it.
-- Applied after 261122-video-localize-mode.sql.

BEGIN;

GRANT SELECT (localize_mode) ON videos TO localization;

COMMIT;

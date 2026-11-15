-- Where a collection puts the subtitle cover box, as shares of the frame. NULL
-- keeps the box found from the source subtitles, as before. The localization
-- worker reads these at dispatch and, for videos still awaiting review, when it
-- makes a new review revision; captions are centred in the box, so they move
-- with it.

BEGIN;

ALTER TABLE collections
  -- Shift of the box centre, % of frame height; positive moves it down.
  ADD COLUMN cover_offset_pct smallint CHECK (cover_offset_pct BETWEEN -50 AND 50),
  -- Box width, % of frame width, centred.
  ADD COLUMN cover_width_pct  smallint CHECK (cover_width_pct BETWEEN 20 AND 100),
  -- Box height, % of frame height.
  ADD COLUMN cover_height_pct smallint CHECK (cover_height_pct BETWEEN 3 AND 50);

COMMIT;

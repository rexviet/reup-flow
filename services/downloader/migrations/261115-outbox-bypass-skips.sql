-- A run the localization ended early on purpose (a video judged an advertisement, or any
-- later reason of the same kind) can be gone on from with the reason set aside. The
-- dashboard sets `bypass_skips` when it puts the row back to `pending` under the same
-- generation; the starter then resets the run to the check that ended it, and the check
-- reads this column and lets the video through. A new generation starts with it off.
-- A file with no video stream is never let through: its check runs before the reset point.

BEGIN;

ALTER TABLE localization_outbox
  ADD COLUMN bypass_skips boolean NOT NULL DEFAULT false;

COMMIT;

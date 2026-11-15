-- Rollback for 261112-dashboard-intake.sql.
--
-- Refuses while any outbox row is still 'held': those videos have not been sent
-- to localization yet and the old check would reject the state. Release or
-- dispatch them first. Back the database up before running this; the new tables
-- (decisions, publications, requests, action log) are dropped with their data.
-- No CASCADE, so an unexpected dependent object fails loudly.

BEGIN;

DO $$ BEGIN
  IF EXISTS (SELECT FROM localization_outbox WHERE state = 'held') THEN
    RAISE EXCEPTION 'localization_outbox still has held rows; release them before rolling back';
  END IF;
END $$;

DROP TABLE dashboard_actions;
DROP TABLE studio_open_requests;
DROP TABLE publications;
DROP TABLE review_decisions;

ALTER TABLE videos DROP COLUMN localize_on_done;
ALTER TABLE profiles DROP COLUMN auto_localize;

ALTER TABLE localization_outbox DROP CONSTRAINT localization_outbox_state_check;
ALTER TABLE localization_outbox
  ADD CONSTRAINT localization_outbox_state_check
  CHECK (state IN ('pending','dispatched','rejected','dead'));

COMMIT;

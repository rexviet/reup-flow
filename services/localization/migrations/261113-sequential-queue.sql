-- Runs held videos one after another. A video is released from `held` to `pending` only
-- when nothing released by this queue is still in flight; the end of that video's run (or
-- a dispatch that gave up) releases the next one, so no process has to poll.
--
-- The events are database triggers:
--   * `localization.runs` written for the in-flight video: the `publish` activity writes it
--     as the last step of every workflow, for every outcome (ready_for_review, needs_human,
--     failed, skipped_*), so a failed video lets the next one start.
--   * the in-flight video's outbox row becoming `rejected` or `dead`: it never reached a
--     workflow, so no run will ever be written.
--   * an outbox row becoming `held`: starts the chain when nothing is in flight.
-- A workflow that dies without writing a run (execution timeout, cancelled in Temporal)
-- leaves its video in flight. The next event after 14 days clears it; sooner, run
--   DELETE FROM auto_queue_inflight; SELECT auto_queue_release_next();
--
-- Only videos this queue released count as in flight, so a video localized by hand runs
-- beside the chain without advancing it. Videos go oldest upload first: an id grows with
-- upload time, so ordering by (length, text) is numeric order without a cast.
--
-- (A `BEGIN` that starts a line is refused by scripts/apply-migrations.sh, hence the
-- `BEGIN` at the end of a line inside the functions.)
-- The functions run as their owner (the migration role) because the `localization` role,
-- whose writes fire the triggers, may not change the outbox.

BEGIN;

CREATE TABLE auto_queue_inflight (
  one          boolean     PRIMARY KEY DEFAULT true CHECK (one),
  aweme_id     text        NOT NULL,
  generation   integer     NOT NULL,
  released_at  timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION auto_queue_release_next() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  next_id  text;
  next_gen integer; BEGIN
  PERFORM pg_advisory_xact_lock(261113, 1);
  DELETE FROM auto_queue_inflight WHERE released_at < now() - interval '14 days';
  IF EXISTS (SELECT FROM auto_queue_inflight) THEN
    RETURN;
  END IF;
  SELECT aweme_id, generation INTO next_id, next_gen
    FROM localization_outbox
   WHERE state = 'held'
   ORDER BY length(aweme_id), aweme_id
   LIMIT 1
     FOR UPDATE SKIP LOCKED;
  IF next_id IS NULL THEN
    RETURN;
  END IF;
  UPDATE localization_outbox SET state = 'pending', next_attempt_at = now()
   WHERE aweme_id = next_id;
  INSERT INTO auto_queue_inflight (aweme_id, generation) VALUES (next_id, next_gen);
  INSERT INTO dashboard_actions (aweme_id, action, detail)
  VALUES (next_id, 'localize', '{"source":"auto_queue"}');
END $$;

CREATE FUNCTION auto_queue_on_finish() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$ BEGIN
  DELETE FROM auto_queue_inflight WHERE aweme_id = NEW.aweme_id AND generation = NEW.generation;
  IF FOUND THEN
    PERFORM auto_queue_release_next();
  END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION auto_queue_on_held() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$ BEGIN
  PERFORM auto_queue_release_next();
  RETURN NULL;
END $$;

REVOKE ALL ON FUNCTION auto_queue_release_next() FROM PUBLIC;
REVOKE ALL ON FUNCTION auto_queue_on_finish() FROM PUBLIC;
REVOKE ALL ON FUNCTION auto_queue_on_held() FROM PUBLIC;

CREATE TRIGGER auto_queue_run_written
  AFTER INSERT OR UPDATE ON localization.runs
  FOR EACH ROW EXECUTE FUNCTION auto_queue_on_finish();

CREATE TRIGGER auto_queue_dispatch_gave_up
  AFTER UPDATE OF state ON localization_outbox
  FOR EACH ROW WHEN (NEW.state IN ('rejected', 'dead'))
  EXECUTE FUNCTION auto_queue_on_finish();

CREATE TRIGGER auto_queue_row_held
  AFTER INSERT OR UPDATE OF state ON localization_outbox
  FOR EACH ROW WHEN (NEW.state = 'held')
  EXECUTE FUNCTION auto_queue_on_held();

COMMIT;

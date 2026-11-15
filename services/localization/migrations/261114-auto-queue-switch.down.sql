-- Rollback for 261114-auto-queue-switch.sql: the queue runs unconditionally again.

BEGIN;

CREATE OR REPLACE FUNCTION auto_queue_release_next() RETURNS void
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

DROP TABLE auto_queue_settings;

COMMIT;

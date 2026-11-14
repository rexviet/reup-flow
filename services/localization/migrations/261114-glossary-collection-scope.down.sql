-- Rollback for 261114-glossary-collection-scope.sql: the glossary goes back to
-- one term per (channel, term_zh).
--
-- Several collections of one channel (its channel collection and its 合集) may
-- each hold the same term. For each (channel, term_zh) the row of the channel
-- collection is kept; without one, the earliest created row. Terms of a manual
-- collection that belongs to no channel have nowhere to go and are dropped. A
-- NOTICE reports how many rows were dropped. Back the database up first.
--
-- Run before 261114-collections.down.sql, which drops the collections.

BEGIN;

DO $$
DECLARE
  dropped bigint; BEGIN
  WITH ranked AS (
    SELECT g.ctid AS row_id,
           c.sec_user_id,
           row_number() OVER (
             PARTITION BY c.sec_user_id, g.term_zh
             ORDER BY (c.kind = 'channel') DESC, g.created_at, g.collection_id
           ) AS rank
      FROM localization.glossary g
      JOIN public.collections c ON c.id = g.collection_id
  )
  DELETE FROM localization.glossary g
   USING ranked r
   WHERE g.ctid = r.row_id AND (r.sec_user_id IS NULL OR r.rank > 1);
  GET DIAGNOSTICS dropped = ROW_COUNT;
  RAISE NOTICE 'glossary rollback dropped % rows that do not fit one term per channel', dropped;
END $$;

ALTER TABLE localization.glossary DROP CONSTRAINT glossary_collection_id_fkey;
ALTER TABLE localization.glossary ADD COLUMN channel_id text;

UPDATE localization.glossary g SET channel_id = c.sec_user_id
  FROM public.collections c
 WHERE c.id = g.collection_id;

ALTER TABLE localization.glossary DROP CONSTRAINT glossary_pkey;
ALTER TABLE localization.glossary DROP COLUMN collection_id;
ALTER TABLE localization.glossary ALTER COLUMN channel_id SET NOT NULL;
ALTER TABLE localization.glossary
  ADD CONSTRAINT glossary_pkey PRIMARY KEY (channel_id, term_zh);

COMMIT;

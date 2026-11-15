-- The glossary is kept per collection instead of per channel.
--
-- Each existing term moves to the channel collection of its channel, created by
-- 261114-collections.sql, which must be applied first. A term whose channel has
-- no collection stops the migration with a count: an operator decides whether
-- that channel's profile is missing or the terms are stale.
--
-- The foreign key to public.collections makes a write for a collection that
-- does not exist fail instead of leaving an orphan term. Terms a person sets on
-- the dashboard are stored with workflow_id = 'dashboard' and
-- source_aweme_id = 'dashboard'; no column is added for them.
--
-- Raw SQL, applied by an admin. Apply in the maintenance window with every
-- service stopped: the worker code that runs after it looks terms up by
-- collection.

BEGIN;

DO $$
DECLARE
  unmatched bigint; BEGIN
  SELECT count(*) INTO unmatched FROM localization.glossary g
   WHERE NOT EXISTS (SELECT FROM public.collections c
                      WHERE c.kind = 'channel' AND c.external_id = g.channel_id);
  IF unmatched > 0 THEN
    RAISE EXCEPTION '% glossary rows name a channel that has no collection; reconcile them, then migrate again', unmatched;
  END IF;
END $$;

ALTER TABLE localization.glossary ADD COLUMN collection_id uuid;

UPDATE localization.glossary g SET collection_id = c.id
  FROM public.collections c
 WHERE c.kind = 'channel' AND c.external_id = g.channel_id;

ALTER TABLE localization.glossary DROP CONSTRAINT glossary_pkey;
ALTER TABLE localization.glossary DROP COLUMN channel_id;
ALTER TABLE localization.glossary ALTER COLUMN collection_id SET NOT NULL;
ALTER TABLE localization.glossary
  ADD CONSTRAINT glossary_pkey PRIMARY KEY (collection_id, term_zh);
ALTER TABLE localization.glossary
  ADD CONSTRAINT glossary_collection_id_fkey
  FOREIGN KEY (collection_id) REFERENCES public.collections (id);

COMMIT;

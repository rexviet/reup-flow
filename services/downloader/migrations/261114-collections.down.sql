-- Rollback for 261114-collections.sql.
--
-- Roll back 261114-glossary-collection-scope.sql first: the glossary references
-- collections, and without CASCADE the DROP below fails loudly instead of
-- taking glossary rows with it.
--
-- Data loss: every collection (with its settings and save folder), every
-- video's collection, 合集 episode and manual choice, every job's hand-picked
-- collection, and every folder export request are dropped. Back the database
-- up before running this.

BEGIN;

DROP TABLE folder_exports;
ALTER TABLE jobs DROP COLUMN collection_id;
DROP INDEX videos_collection_recent_idx;
ALTER TABLE videos DROP COLUMN collection_manual;
ALTER TABLE videos DROP COLUMN mix_episode;
ALTER TABLE videos DROP COLUMN collection_id;
DROP TABLE collections;

COMMIT;

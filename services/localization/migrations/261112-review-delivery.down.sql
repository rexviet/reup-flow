-- Rollback for 261112-review-delivery.sql. Stop the review worker first; the
-- three tables are dropped with their data, so back up if it matters.

BEGIN;

DROP TABLE localization.host_workers;
DROP TABLE localization.review_status;
DROP TABLE localization.final_renders;

COMMIT;

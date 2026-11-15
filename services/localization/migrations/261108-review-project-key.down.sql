-- Back up first; stop all review-project consumers before removing this pointer.
ALTER TABLE localization.runs DROP COLUMN project_key;

-- Apply only during review rollout, after backing up the database.
ALTER TABLE localization.runs ADD COLUMN project_key text;

REVOKE SELECT (aweme_id, sec_user_id, storage_path) ON videos FROM localization;
REVOKE SELECT (aweme_id, generation, state, next_attempt_at, dispatch_attempts)
  ON localization_outbox FROM localization;
REVOKE UPDATE (state, next_attempt_at, dispatch_attempts, last_error, dispatched_at, workflow_id)
  ON localization_outbox FROM localization;

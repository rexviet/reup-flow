-- Recognize only complete, known schema surfaces. Partial surfaces need reconciliation.
CREATE FUNCTION pg_temp.migration_present(migration text) RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
  expected text[];
  item text;
  parts text[];
  found integer := 0;
  present boolean;
BEGIN
  CASE migration
    WHEN '260824-init-schema.sql' THEN expected := ARRAY[
      'public.profiles.sec_user_id:text', 'public.profiles.nickname:text', 'public.profiles.last_synced_at:timestamp with time zone',
      'public.jobs.id:uuid', 'public.jobs.sec_user_id:text', 'public.jobs.status:text', 'public.jobs.total_videos:integer',
      'public.jobs.enumerated_count:integer','public.jobs.downloaded_count:integer','public.jobs.failed_count:integer',
      'public.jobs.max_cursor:bigint','public.jobs.lease_expires_at:timestamp with time zone','public.jobs.error:text',
      'public.jobs.created_at:timestamp with time zone','public.jobs.updated_at:timestamp with time zone',
      'public.videos.aweme_id:text','public.videos.sec_user_id:text','public.videos.desc:text','public.videos.create_time:timestamp with time zone',
      'public.videos.duration_ms:integer','public.videos.play_addr:text','public.videos.url_expires_at:timestamp with time zone',
      'public.videos.storage_path:text','public.videos.status:text','public.videos.attempts:integer','public.videos.last_error:text',
      'public.videos.lease_expires_at:timestamp with time zone',
      'public.profiles_pkey','public.jobs_pkey','public.videos_pkey',
      'public.videos_pending_claim_idx','public.videos_downloading_lease_idx','public.videos_profile_recent_idx',
      'public.jobs_status_created_idx','public.jobs_enumerating_lease_idx','public.jobs_sec_user_id_idx','public.jobs@jobs_sec_user_id_fkey'];
    WHEN '261026-localization-outbox.sql' THEN expected := ARRAY[
      'public.localization_outbox.aweme_id:text','public.localization_outbox.generation:integer','public.localization_outbox.state:text',
      'public.localization_outbox.enqueued_at:timestamp with time zone','public.localization_outbox.next_attempt_at:timestamp with time zone',
      'public.localization_outbox.dispatched_at:timestamp with time zone','public.localization_outbox.workflow_id:text',
      'public.localization_outbox.dispatch_attempts:integer','public.localization_outbox.last_error:text',
      'public.localization_outbox_pkey','public.localization_outbox_due_idx',
      'public.localization_outbox@localization_outbox_aweme_id_fkey','public.localization_outbox@localization_outbox_state_check'];
    WHEN '261026-localization-core.sql' THEN expected := ARRAY[
      'localization.glossary.channel_id:text','localization.glossary.term_zh:text','localization.glossary.term_vi:text',
      'localization.glossary.note:text','localization.glossary.source_aweme_id:text','localization.glossary.workflow_id:text',
      'localization.glossary.created_at:timestamp with time zone',
      'localization.voice_rates.voice_hash:text','localization.voice_rates.syllables_per_sec:double precision',
      'localization.voice_rates.measured_at:timestamp with time zone',
      'localization.runs.aweme_id:text','localization.runs.generation:integer','localization.runs.workflow_id:text',
      'localization.runs.status:text','localization.runs.output_key:text','localization.runs.review_key:text',
      'localization.runs.verdict:jsonb','localization.runs.review_rounds:integer','localization.runs.needs_human:boolean',
      'localization.runs.updated_at:timestamp with time zone',
      'localization.glossary_pkey','localization.voice_rates_pkey','localization.runs_pkey',
      'localization.glossary@glossary_term_vi_check','localization.glossary@glossary_note_check',
      'localization.voice_rates@voice_rates_syllables_per_sec_check','localization.runs@runs_generation_check',
      'localization.runs@runs_review_rounds_check'];
    WHEN '261026-outbox-starter-grants.sql' THEN
      -- Idempotent grants are safe to actually apply, never infer them from tables.
      RETURN false;
    WHEN '261027-llm-cost.sql' THEN expected := ARRAY[
      'localization.llm_calls.generation_id:text','localization.llm_calls.aweme_id:text','localization.llm_calls.generation:integer',
      'localization.llm_calls.stage:text','localization.llm_calls.model:text','localization.llm_calls.prompt_tokens:integer',
      'localization.llm_calls.completion_tokens:integer','localization.llm_calls.cost_usd:numeric(14,8)',
      'localization.llm_calls.created_at:timestamp with time zone','localization.llm_calls_pkey','localization.llm_calls_run_idx',
      'localization.runs.llm_cost_usd:numeric(14,8)'];
    WHEN '261028-llm-call-provider.sql' THEN expected := ARRAY['localization.llm_calls.provider:text'];
    WHEN '261108-review-project-key.sql' THEN expected := ARRAY['localization.runs.project_key:text'];
    ELSE RAISE EXCEPTION 'Unknown migration %, reconcile manually', migration;
  END CASE;
  FOREACH item IN ARRAY expected LOOP
    parts := string_to_array(split_part(item, ':', 1), '.');
    IF position('@' in item) > 0 THEN
      SELECT EXISTS (SELECT FROM pg_constraint WHERE conrelid = to_regclass(split_part(item, '@', 1))
        AND conname = split_part(item, '@', 2)) INTO present;
    ELSIF array_length(parts, 1) = 2 THEN
      present := to_regclass(item) IS NOT NULL;
    ELSE
      SELECT EXISTS (
        SELECT FROM pg_attribute a WHERE a.attrelid = to_regclass(parts[1] || '.' || parts[2])
          AND a.attname = parts[3] AND NOT a.attisdropped
          AND format_type(a.atttypid, a.atttypmod) = split_part(item, ':', 2)
      ) INTO present;
      -- An existing column with a different type is partial, never absent.
      IF NOT present AND EXISTS (
        SELECT FROM pg_attribute a WHERE a.attrelid = to_regclass(parts[1] || '.' || parts[2])
          AND a.attname = parts[3] AND NOT a.attisdropped
      ) THEN RAISE EXCEPTION 'Unrecognized schema for %, reconcile manually', item; END IF;
    END IF;
    IF present THEN found := found + 1; END IF;
  END LOOP;
  IF found > 0 AND found < cardinality(expected) THEN
    RAISE EXCEPTION 'Partial schema for %, reconcile manually', migration;
  END IF;
  RETURN found = cardinality(expected);
END $$;

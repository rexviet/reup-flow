#!/usr/bin/env bash
# Real Postgres; touches only databases created here. No live truncate/drop.
set -euo pipefail
cd "$(dirname "$0")/../.."
name="reup_migration_test_$(date +%s)_$$"
psql_() { docker compose exec -T postgres sh -c 'db=$1; shift; psql -X -U "$POSTGRES_USER" -d "$db" -v ON_ERROR_STOP=1 -qAt "$@"' sh "$name" "$@"; }
cleanup() { docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$1"' sh "$name"; }
trap cleanup EXIT
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$name"
run() { MIGRATION_TEST_DATABASE="$name" bash scripts/apply-migrations.sh; }
assert_sql() { local got; got=$(printf '%s\n' "$1" | psql_); [ "$got" = "$2" ] || { echo "assertion failed: expected $2 got $got" >&2; exit 1; }; }
run
assert_sql 'SELECT count(*) FROM public.applied_migrations' 9
run
assert_sql 'SELECT count(*) FROM public.applied_migrations' 9

# Dashboard migrations: outbox accepts 'held' only, and a down/up round trip works.
printf "INSERT INTO public.profiles(sec_user_id) VALUES ('p'); INSERT INTO public.videos(aweme_id,sec_user_id,create_time) VALUES ('h1','p',now()),('h2','p',now());\n" | psql_
printf "INSERT INTO public.localization_outbox(aweme_id,state) VALUES ('h1','held');\n" | psql_
if printf "INSERT INTO public.localization_outbox(aweme_id,state) VALUES ('h2','bogus');\n" | psql_ 2>/dev/null; then
  echo 'bogus outbox state unexpectedly accepted' >&2; exit 1
fi
if psql_ -1 < services/downloader/migrations/261112-dashboard-intake.down.sql 2>/dev/null; then
  echo 'down unexpectedly passed with held rows' >&2; exit 1
fi
printf "DELETE FROM public.localization_outbox;\n" | psql_
psql_ -1 < services/localization/migrations/261112-review-delivery.down.sql >/dev/null 2>&1
psql_ -1 < services/downloader/migrations/261112-dashboard-intake.down.sql >/dev/null 2>&1
assert_sql "SELECT count(*) FROM pg_tables WHERE (schemaname,tablename) IN (('public','review_decisions'),('localization','final_renders'))" 0
printf "DELETE FROM public.applied_migrations WHERE name IN ('261112-dashboard-intake.sql','261112-review-delivery.sql');\n" | psql_
run
assert_sql 'SELECT count(*) FROM public.applied_migrations' 9

# The worker role (NOSUPERUSER) writes what it owns, reads decisions, and nothing more.
printf "INSERT INTO public.studio_open_requests(aweme_id,generation) VALUES ('h1',1); INSERT INTO public.review_decisions(aweme_id,generation,decision,approved_revision) VALUES ('h1',1,'approved','rev1');\n" | psql_
cat <<'SQL' | psql_
SET ROLE localization;
INSERT INTO localization.final_renders(aweme_id,generation,approved_revision) VALUES ('h1',1,'rev1');
UPDATE localization.final_renders SET state='rendering', attempts=attempts+1, claim=gen_random_uuid(), lease_expires_at=now()+interval '5 min' WHERE state='queued';
UPDATE localization.final_renders SET state='rendered', final_key='k.mp4', srt_key='k.srt', rendered_at=now() WHERE aweme_id='h1';
INSERT INTO localization.review_status(aweme_id,generation,revision,cue_count,mismatch_count,dirty) VALUES ('h1',1,'rev1',3,0,false)
  ON CONFLICT (aweme_id,generation) DO UPDATE SET revision=EXCLUDED.revision, checked_at=now();
INSERT INTO localization.host_workers(name,info) VALUES ('host','{}') ON CONFLICT (name) DO UPDATE SET seen_at=now(), info=EXCLUDED.info;
UPDATE public.studio_open_requests SET state='done', error=NULL, handled_at=now() WHERE state='pending';
SQL
assert_sql "SET ROLE localization; SELECT decision FROM public.review_decisions WHERE aweme_id='h1'" approved
for denied in \
  "INSERT INTO public.review_decisions(aweme_id,generation,decision) VALUES ('h1',2,'approved')" \
  "UPDATE public.review_decisions SET decision='returned'" \
  "DELETE FROM public.review_decisions" \
  "UPDATE public.studio_open_requests SET aweme_id='x'" \
  "INSERT INTO public.studio_open_requests(aweme_id,generation) VALUES ('h1',3)" \
  "SELECT * FROM public.publications"; do
  if err=$(printf 'SET ROLE localization; %s;\n' "$denied" | psql_ 2>&1); then
    echo "worker role unexpectedly allowed: $denied" >&2; exit 1
  fi
  case "$err" in *'permission denied for table'*) ;; *) echo "not a privilege error for: $denied: $err" >&2; exit 1 ;; esac
done
printf "TRUNCATE public.review_decisions, public.studio_open_requests, localization.final_renders, localization.review_status, localization.host_workers, public.videos, public.profiles CASCADE;\n" | psql_
printf '%s\n' "INSERT INTO localization.runs(aweme_id,generation,workflow_id,status,review_key) VALUES ('7001',1,'old','needs_human','review/old.mp4');" | psql_
backup="backups/migration-fixture-$$.dump"
(umask 077; docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$1" -Fc' sh "$name" > "$backup")
shasum -a 256 "$backup" > "$backup.sha256"
cat services/localization/migrations/261108-review-project-key.down.sql | psql_
printf 'DROP TABLE public.applied_migrations;\n' | psql_
run
assert_sql "SELECT status || ':' || review_key || ':' || (project_key IS NULL) FROM localization.runs" 'needs_human:review/old.mp4:true'
cat services/localization/migrations/261108-review-project-key.down.sql | psql_
cat <<'SQL' | psql_
DELETE FROM public.applied_migrations WHERE name = '261108-review-project-key.sql';
CREATE FUNCTION public.reject_ledger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
  IF NEW.name = '261108-review-project-key.sql' THEN
    PERFORM pg_terminate_backend(pg_backend_pid());
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER reject_ledger BEFORE INSERT ON public.applied_migrations FOR EACH ROW EXECUTE FUNCTION public.reject_ledger();
SQL
if run; then echo 'injected crash unexpectedly passed' >&2; exit 1; fi
assert_sql "SELECT count(*) FROM information_schema.columns WHERE table_schema='localization' AND table_name='runs' AND column_name='project_key'" 0
assert_sql "SELECT count(*) FROM public.applied_migrations WHERE name='261108-review-project-key.sql'" 0
printf 'DROP TRIGGER reject_ledger ON public.applied_migrations; DROP FUNCTION public.reject_ledger();\n' | psql_
run
printf "DROP TABLE public.applied_migrations; ALTER TABLE public.jobs DROP COLUMN error;\n" | psql_
if run; then echo 'partial baseline unexpectedly passed' >&2; exit 1; fi
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$1" --clean --if-exists --exit-on-error' sh "$name" < "$backup"
assert_sql "SELECT status || ':' || review_key FROM localization.runs" 'needs_human:review/old.mp4'
run
echo 'PASS: fresh, existing ledger, ledger-less upgrade, second run, DDL/ledger failure atomicity, unknown schema rejection, down and restore'

#!/usr/bin/env bash
# Real Postgres; touches only databases created here. No live truncate/drop.
set -euo pipefail
cd "$(dirname "$0")/../.."
name="reup_migration_test_$(date +%s)_$$"
psql_() { docker compose exec -T postgres sh -c 'psql -X -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 -qAt' sh "$name"; }
cleanup() { docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$1"' sh "$name"; }
trap cleanup EXIT
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$name"
run() { MIGRATION_TEST_DATABASE="$name" bash scripts/apply-migrations.sh; }
assert_sql() { local got; got=$(printf '%s\n' "$1" | psql_); [ "$got" = "$2" ] || { echo "assertion failed: expected $2 got $got" >&2; exit 1; }; }
run
assert_sql 'SELECT count(*) FROM public.applied_migrations' 7
run
assert_sql 'SELECT count(*) FROM public.applied_migrations' 7
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

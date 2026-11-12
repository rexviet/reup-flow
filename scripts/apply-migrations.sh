#!/usr/bin/env bash
# Applies each migration and its ledger entry atomically. Backups stay local.
set -euo pipefail
cd "$(dirname "$0")/.."

# Tests may target only their own disposable database, never an arbitrary DSN.
if [ -n "${MIGRATION_TEST_DATABASE:-}" ] && [[ ! "$MIGRATION_TEST_DATABASE" =~ ^reup_migration_test_[a-z0-9_]+$ ]]; then
  echo 'Invalid disposable migration database name' >&2
  exit 2
fi
MIGRATIONS=(
  services/downloader/migrations/260824-init-schema.sql
  services/downloader/migrations/261026-localization-outbox.sql
  services/localization/migrations/261026-localization-core.sql
  services/localization/migrations/261026-outbox-starter-grants.sql
  services/localization/migrations/261027-llm-cost.sql
  services/localization/migrations/261028-llm-call-provider.sql
  services/localization/migrations/261108-review-project-key.sql
  services/downloader/migrations/261112-dashboard-intake.sql
  services/localization/migrations/261112-review-delivery.sql
)
psql_() {
  docker compose exec -T -e MIGRATION_TEST_DATABASE="${MIGRATION_TEST_DATABASE:-}" postgres sh -c \
    'psql -X -U "$POSTGRES_USER" -d "${MIGRATION_TEST_DATABASE:-$POSTGRES_DB}" -v ON_ERROR_STOP=1 -qAt "$@"' sh "$@"
}

# Refuse unsupported transaction control before backing up or applying anything.
for f in "${MIGRATIONS[@]}"; do
  awk '
    /^[[:space:]]*--/ || /^[[:space:]]*$/ {next}
    {line=toupper($0)}
    line ~ /^[[:space:]]*BEGIN;[[:space:]]*$/ {if (seen || started) exit 1; started=1; next}
    line ~ /^[[:space:]]*COMMIT;[[:space:]]*$/ {if (!started || ended) exit 1; ended=1; next}
    line ~ /^[[:space:]]*(BEGIN|COMMIT|ROLLBACK|START[[:space:]]+TRANSACTION)([[:space:];]|$)/ {exit 1}
    {if (ended) exit 1; seen=1}
    END {if (started && !ended) exit 1}
  ' "$f" || { echo "Unsupported migration transaction control: $f" >&2; exit 2; }
done

# A verified restore is a prerequisite, including for an already-migrated database.
mkdir -p backups
backup="backups/migrations-$(date +%Y%m%d-%H%M%S)-$$.dump"
(umask 077; docker compose exec -T -e MIGRATION_TEST_DATABASE="${MIGRATION_TEST_DATABASE:-}" postgres sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "${MIGRATION_TEST_DATABASE:-$POSTGRES_DB}" -Fc' > "$backup")
shasum -a 256 "$backup" > "$backup.sha256"
clone="reup_migration_restore_$(date +%s)_$$"
cleanup() {
  docker compose exec -T postgres sh -c \
    'dropdb -U "$POSTGRES_USER" --if-exists "$1"' sh "$clone" >/dev/null
}
trap cleanup EXIT
docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$clone"
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$1" --exit-on-error' sh "$clone" < "$backup"
echo "backup and restore verified: $backup"

# One connection, transaction-scoped advisory lock for every migration, no race
# between observing schema/ledger and committing either adoption or DDL.
{
  cat scripts/migration-evidence.sql
  for f in "${MIGRATIONS[@]}"; do
    name=$(basename "$f")
    cat <<SQL
BEGIN;
SELECT pg_advisory_xact_lock(261108, 5);
CREATE TABLE IF NOT EXISTS public.applied_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
SELECT EXISTS (SELECT FROM public.applied_migrations WHERE name = '$name') AS recorded \gset
\if :recorded
\echo skip $name
\else
SELECT pg_temp.migration_present('$name') AS present \gset
\if :present
\echo adopt $name
\else
\echo apply $name
SQL
    # Existing files remain usable standalone. Only boundary transaction lines
    # are stripped; any other transaction control is rejected before execution.
    awk '/^[[:space:]]*(BEGIN|COMMIT);[[:space:]]*$/ {next} {print}' "$f"
    cat <<SQL

\endif
INSERT INTO public.applied_migrations(name) VALUES ('$name');
\endif
COMMIT;
SQL
  done
} | psql_
echo 'migrations: done'

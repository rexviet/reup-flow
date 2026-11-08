#!/usr/bin/env bash
# Applies the SQL migrations to the compose Postgres, in order, once each.
#
# Compose does not apply them: a fresh Postgres volume is empty, so every route that
# touches the database fails until these have run. Which file has been applied is
# recorded in public.applied_migrations, so the script can be run again safely.
#
# A database that already has the tables but no ledger (restored from another
# machine) is taken to have every migration applied; the ledger is then seeded.
#
# Needs the `postgres` and `db-init` services to have run: the migrations refer to
# the `localization` role that db-init creates.
set -euo pipefail
cd "$(dirname "$0")/.."

MIGRATIONS=(
  services/downloader/migrations/260824-init-schema.sql
  services/downloader/migrations/261026-localization-outbox.sql
  services/localization/migrations/261026-localization-core.sql
  services/localization/migrations/261026-outbox-starter-grants.sql
  services/localization/migrations/261027-llm-cost.sql
  services/localization/migrations/261028-llm-call-provider.sql
)

psql_() {
  docker compose exec -T postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -qAt "$@"' sh "$@"
}

ledger_exists=$(psql_ -c "SELECT to_regclass('public.applied_migrations') IS NOT NULL")
if [ "$ledger_exists" != "t" ]; then
  tables_exist=$(psql_ -c "SELECT to_regclass('public.videos') IS NOT NULL")
  psql_ -c "CREATE TABLE public.applied_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
  if [ "$tables_exist" = "t" ]; then
    echo "The database already has its tables; recording every migration as applied."
    for f in "${MIGRATIONS[@]}"; do
      psql_ -c "INSERT INTO public.applied_migrations (name) VALUES ('$(basename "$f")')"
    done
  fi
fi

for f in "${MIGRATIONS[@]}"; do
  name=$(basename "$f")
  done_already=$(psql_ -c "SELECT EXISTS (SELECT 1 FROM public.applied_migrations WHERE name = '$name')")
  if [ "$done_already" = "t" ]; then
    echo "skip   $name"
    continue
  fi
  echo "apply  $name"
  # A file that opens its own transaction cannot also run under psql -1.
  if grep -qiE '^[[:space:]]*BEGIN[[:space:]]*;' "$f"; then
    psql_ < "$f" > /dev/null
  else
    psql_ -1 < "$f" > /dev/null
  fi
  psql_ -c "INSERT INTO public.applied_migrations (name) VALUES ('$name')"
done
echo "migrations: done"

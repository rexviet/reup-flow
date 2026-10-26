#!/bin/sh
# Idempotent. Runs in the one-shot `db-init` service, NOT via
# /docker-entrypoint-initdb.d: that directory only executes on an empty data
# volume, and the reup volume already holds data.
#
# Gives every system its own non-superuser role and database, so a bug or a
# compromise in Temporal or Langfuse cannot read the `reup` schema.
set -eu

: "${PGHOST:?}" "${PGUSER:?}" "${PGPASSWORD:?}" "${POSTGRES_DB:?}"
: "${TEMPORAL_DB_PASSWORD:?}" "${LANGFUSE_DB_PASSWORD:?}" "${LOCALIZATION_DB_PASSWORD:?}"

psql_admin() { psql -X -v ON_ERROR_STOP=1 -d postgres "$@"; }

# $1 role, $2 password. CREATE when absent, always re-assert attributes and
# password so a rotated .env value takes effect on the next run.
ensure_role() {
  psql_admin -v role="$1" -v pw="$2" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'role')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
              :'role', :'pw') \gexec
SQL
}

# $1 database, $2 owner. CREATE DATABASE cannot run inside a function or DO
# block, hence \gexec.
ensure_database() {
  psql_admin -v db="$1" -v owner="$2" <<'SQL'
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'owner')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'db', :'owner') \gexec
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
}

ensure_role temporal     "$TEMPORAL_DB_PASSWORD"
ensure_role langfuse     "$LANGFUSE_DB_PASSWORD"
ensure_role localization "$LOCALIZATION_DB_PASSWORD"

ensure_database temporal            temporal
ensure_database temporal_visibility temporal
ensure_database langfuse            langfuse

# The existing app database. Superusers (the reup user) bypass CONNECT checks,
# so the downloader keeps working. Per-table grants for `localization` are
# added by the phases that create the tables.
psql_admin -v db="$POSTGRES_DB" <<'SQL'
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO localization', :'db') \gexec
SQL

echo "db-init: ok"

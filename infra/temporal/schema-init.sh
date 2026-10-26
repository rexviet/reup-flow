#!/bin/sh
# Idempotent. Applies the Temporal persistence and visibility schemas as the
# `temporal` role (never a superuser) and exits. Runs before the server starts.
set -eu
: "${SQL_HOST:?}" "${SQL_USER:?}" "${SQL_PASSWORD:?}"

SCHEMA=/etc/temporal/schema/postgresql/v12
tool() { temporal-sql-tool --plugin postgres12 --ep "$SQL_HOST" -p 5432 -u "$SQL_USER" --pw "$SQL_PASSWORD" "$@"; }

for pair in "temporal:temporal" "temporal_visibility:visibility"; do
  db=${pair%%:*}; dir=${pair##*:}
  tool --db "$db" setup-schema -v 0.0 >/dev/null 2>&1 || true
  tool --db "$db" update-schema -d "$SCHEMA/$dir/versioned"
done
echo "temporal-admin-tools: schema ok"

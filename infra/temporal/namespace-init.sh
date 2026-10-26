#!/bin/sh
# Idempotent. The plain temporalio/server image does not create the `default`
# namespace, so create it (or update its retention) once the frontend is up.
set -eu
: "${TEMPORAL_ADDRESS:?}" "${TEMPORAL_NAMESPACE:?}"
RETENTION=${TEMPORAL_RETENTION:-30d}

i=0
until temporal operator cluster health --address "$TEMPORAL_ADDRESS" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "temporal-namespace-init: server unreachable" >&2; exit 1; }
  sleep 2
done

if temporal operator namespace describe --address "$TEMPORAL_ADDRESS" -n "$TEMPORAL_NAMESPACE" >/dev/null 2>&1; then
  temporal operator namespace update --address "$TEMPORAL_ADDRESS" -n "$TEMPORAL_NAMESPACE" --retention "$RETENTION"
else
  temporal operator namespace create --address "$TEMPORAL_ADDRESS" -n "$TEMPORAL_NAMESPACE" --retention "$RETENTION"
fi
echo "temporal-namespace-init: ok"

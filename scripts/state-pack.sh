#!/usr/bin/env bash
# Moves the stack's state to another machine.
#
#   scripts/state-pack.sh export <out-dir>   on the old machine
#   scripts/state-pack.sh import <in-dir>    on the new one, BEFORE scripts/bootstrap.sh
#
# What travels: the Postgres volume (the app's tables, Temporal's history, Langfuse's
# metadata), the MinIO volume (the media), the ClickHouse volume (Langfuse's traces and
# eval scores), data/douyin, and .env.
#
# The pack holds every secret in .env and the database contents. It is written with
# mode 700 and must be sent over a channel you trust. Never commit it.
#
# The volumes are copied as raw files, so the two machines must have the same CPU
# architecture (arm64 to arm64, x86_64 to x86_64) and the same compose file and image
# versions. The stack is stopped during export so the files are consistent; start it
# again afterwards with `docker compose up -d`.
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="${1:-}"; DIR="${2:-}"
[ -n "$MODE" ] && [ -n "$DIR" ] || { sed -n '2,5p' "$0"; exit 2; }
command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }

project=$(docker compose config 2>/dev/null | awk '/^name:/ {print $2; exit}')
[ -n "$project" ] || { echo "cannot read the compose project name" >&2; exit 1; }
VOLUMES=(postgres_data minio_data clickhouse_data)

case "$MODE" in
  export)
    [ -f .env ] || { echo ".env not found here" >&2; exit 1; }
    mkdir -p "$DIR"; chmod 700 "$DIR"
    echo "stopping the stack so the files are consistent"
    docker compose stop
    for v in "${VOLUMES[@]}"; do
      vol="${project}_${v}"
      docker volume inspect "$vol" >/dev/null 2>&1 || { echo "volume $vol not found" >&2; exit 1; }
      echo "packing $vol"
      docker run --rm -v "$vol":/data:ro -v "$(cd "$DIR" && pwd)":/out alpine \
        tar czf "/out/${v}.tgz" -C /data .
    done
    cp .env "$DIR/env"
    if [ -d data/douyin ]; then tar czf "$DIR/data-douyin.tgz" -C data douyin; fi
    echo "$(uname -m)" > "$DIR/arch"
    ( cd "$DIR" && chmod 600 ./* && { sha256sum ./* 2>/dev/null || shasum -a 256 ./*; } > SHA256SUMS )
    echo
    echo "pack written to $DIR ($(du -sh "$DIR" | cut -f1)). The stack is stopped; restart it with: docker compose up -d"
    ;;

  import)
    [ -d "$DIR" ] || { echo "$DIR is not a directory" >&2; exit 1; }
    [ "$(cat "$DIR/arch" 2>/dev/null || true)" = "$(uname -m)" ] \
      || { echo "the pack was made on $(cat "$DIR/arch" 2>/dev/null || echo unknown); this machine is $(uname -m). Raw volume copies need the same architecture." >&2; exit 1; }
    ( cd "$DIR" && { sha256sum -c SHA256SUMS >/dev/null 2>&1 || shasum -a 256 -c SHA256SUMS >/dev/null 2>&1; } ) \
      || { echo "checksum mismatch: the pack is damaged or incomplete" >&2; exit 1; }
    [ ! -f .env ] || { echo ".env already exists here; move it away first so the pack's one is not mixed with it" >&2; exit 1; }
    if [ -n "$(docker compose ps -q 2>/dev/null)" ]; then
      echo "the stack is running; stop it first (docker compose down)" >&2; exit 1
    fi

    echo "creating the volumes"
    docker compose up --no-start postgres minio clickhouse
    for v in "${VOLUMES[@]}"; do
      vol="${project}_${v}"
      if [ -n "$(docker run --rm -v "$vol":/data alpine sh -c 'ls -A /data | head -1')" ]; then
        echo "volume $vol is not empty; refusing to overwrite it" >&2; exit 1
      fi
    done
    for v in "${VOLUMES[@]}"; do
      vol="${project}_${v}"
      echo "restoring $vol"
      docker run --rm -v "$vol":/data -v "$(cd "$DIR" && pwd)":/in:ro alpine \
        tar xzf "/in/${v}.tgz" -C /data
    done
    cp "$DIR/env" .env; chmod 600 .env
    if [ -f "$DIR/data-douyin.tgz" ]; then mkdir -p data; tar xzf "$DIR/data-douyin.tgz" -C data; fi
    echo
    echo "state restored. Now run scripts/bootstrap.sh (it keeps this .env; pass --host if other machines will browse to this one)."
    echo "Check VOICESTUDIO_URL in .env: a tunnel address from the old machine may no longer be valid."
    ;;

  *) sed -n '2,5p' "$0"; exit 2 ;;
esac

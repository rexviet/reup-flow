"""Read completed project identities for the Mac's local review watcher."""

import argparse
import json
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from localization.config import ConfigError, load_settings

READY_SQL = """
SELECT aweme_id, generation, status
FROM localization.runs
WHERE updated_at >= %s
  AND status IN ('ready_for_review', 'needs_human')
  AND project_key = 'review/' || aweme_id || '/g' || generation::text || '/head.json'
ORDER BY updated_at, aweme_id, generation
"""


def read_ready(conn, since: datetime) -> list[dict]:
    if since.tzinfo is None:
        raise ValueError("since must include a timezone")
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        return conn.execute(READY_SQL, (since,)).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("since", type=datetime.fromisoformat)
    args = parser.parse_args()
    settings = load_settings()
    if settings.loc_database_url is None:
        raise ConfigError("LOC_DATABASE_URL is required")
    with psycopg.connect(
        settings.loc_database_url.get_secret_value(), row_factory=dict_row, connect_timeout=5
    ) as conn:
        print(json.dumps(read_ready(conn, args.since)))


if __name__ == "__main__":
    main()

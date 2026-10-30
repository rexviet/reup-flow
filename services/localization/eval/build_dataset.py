"""Builds the Localizer dataset on Langfuse from runs that were published.

    uv run python eval/build_dataset.py --videos 5 [--dataset localizer-v1]

For each of the newest runs with a wanted status (`published` unless `--status` says
otherwise; table `localization.runs`), the run's history
gives exactly what its Localizer saw and said: the input and the result of
`localizer_context` (the transcript with slots, the burned-in subtitle spans, the
glossary terms) and the lines handed to the first `assemble_script`. One dataset item
per video, keyed by video id, so building again updates the items instead of adding
copies. A run made in the tool-loop mode has no `localizer_context`, and a run whose
transcript is empty gave the Localizer nothing to do; both are skipped.
"""

import argparse
import os
import sys

import psycopg
from common import (
    LANGFUSE,
    TEMPORAL_UI,
    History,
    Langfuse,
    host_database_url,
    load_env,
    payloads,
)

RUNS_SQL = """
SELECT DISTINCT ON (aweme_id) aweme_id, generation, workflow_id, updated_at
FROM localization.runs
WHERE status = ANY(%s)
ORDER BY aweme_id, generation DESC
"""


def newest_runs(database_url: str, statuses: list[str]) -> list[tuple[str, int, str]]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            f"SELECT * FROM ({RUNS_SQL}) r ORDER BY updated_at DESC", (statuses,)
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def item_from_history(history: History) -> tuple[dict, dict, dict] | None:
    """(input, expected output, metadata) of the run's first translation, or None when
    the run did not use the one-request Localizer."""
    contexts = history.activities("localizer_context")
    if not contexts or contexts[0][1] is None or not contexts[0][1]["segments"]:
        return None
    (deps,), context = contexts[0]
    assembled = history.activities("assemble_script")
    lines = assembled[0][0][3] if assembled else None
    started = history.events[0]["workflowExecutionStartedEventAttributes"]
    spec = started["input"]
    (run_input,) = payloads(spec)
    item_input = {
        "aweme_id": deps["aweme_id"],
        "channel_id": deps["channel_id"],
        "syllables_per_sec": deps["syllables_per_sec"],
        "segment_count": deps["segment_count"],
        "context": context,
    }
    metadata = {
        "workflow_id": deps["workflow_id"],
        "generation": run_input["generation"],
        "segments": len(context["segments"]),
        "terms": len(context["terms"]),
    }
    return item_input, (lines or {}), metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--videos", type=int, required=True)
    parser.add_argument("--dataset", default="localizer-v1")
    parser.add_argument(
        "--status",
        default="published",
        help="comma-separated run statuses to take videos from (published, needs_human)",
    )
    parser.add_argument("--langfuse", default=LANGFUSE)
    parser.add_argument("--temporal-ui", default=TEMPORAL_UI)
    parser.add_argument("--database-url", help="defaults to LOC_DATABASE_URL on localhost")
    args = parser.parse_args()

    load_env()
    database_url = args.database_url or host_database_url(os.environ["LOC_DATABASE_URL"])
    langfuse = Langfuse.from_env(args.langfuse)
    langfuse.ensure_dataset(
        args.dataset, "Localizer inputs and first answers of published runs (one per video)."
    )
    added = 0
    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    for aweme_id, generation, workflow_id in newest_runs(database_url, statuses):
        if added == args.videos:
            break
        item = item_from_history(History.fetch(args.temporal_ui, workflow_id))
        if item is None:
            print(f"skip {aweme_id} g{generation}: no one-request Localizer, or no speech")
            continue
        item_input, expected, metadata = item
        langfuse.upsert_item(
            args.dataset, f"{args.dataset}-{aweme_id}", item_input, expected, metadata
        )
        added += 1
        print(f"item {aweme_id} g{generation}: {metadata['segments']} segments")
    print(f"{added} item(s) in dataset {args.dataset}")
    return 0 if added == args.videos else 1


if __name__ == "__main__":
    sys.exit(main())

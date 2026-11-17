"""Compares two generations of one video run by the real workflow: what the subtitle
stages (`detect_subtitles`, `complete_transcript`) cost, how long they took and what
they found, the later generation (the changed code) against the earlier one.

  time       each stage in the Temporal history of the last run that completed it, from
             its first scheduling to its close, retries included
  cost       the stage's `localization.llm_calls` within that same window
  accuracy   the subtitle region (top and bottom, as a share of the frame's height),
             and the lines added from the frames, matched by text

    uv run python eval/subtitle_sampling_generations.py --out ../../data/ocr-bench/sampling-v1 \\
        7702770865292758299:1:2 7703457196239872678:1:2
"""

import argparse
import difflib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from common import TEMPORAL_UI, host_database_url, load_env

STAGES = ("detect_subtitles", "complete_transcript")
# Two readings of one line by two OCR runs differ by a character or two.
SAME_LINE_RATIO = 0.6


def added_lines(doc: dict) -> list[dict]:
    """The segments read from the frames, not heard."""
    return [
        {"start_ms": s["start_ms"], "end_ms": s["end_ms"], "text": s["text"]}
        for s in doc["segments"]
        if s.get("source") == "subtitle"
    ]


def same(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() >= SAME_LINE_RATIO


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _runs(http: httpx.Client, workflow: str) -> list[list[dict]]:
    """The history of every run of `workflow`, oldest first: a resumed or bypassed
    generation runs again under the same workflow id."""
    listed = http.get(
        "/api/v1/namespaces/default/workflows",
        params={"query": f'WorkflowId="{workflow}"'},
    ).json()["executions"]
    runs = []
    for run in sorted(listed, key=lambda e: e["startTime"]):
        events, token = [], None
        while True:
            params = {"maximumPageSize": 1000, "execution.runId": run["execution"]["runId"]}
            body = http.get(
                f"/api/v1/namespaces/default/workflows/{workflow}/history",
                params=params | ({"nextPageToken": token} if token else {}),
            ).json()
            events += body["history"]["events"]
            token = body.get("nextPageToken")
            if not token:
                break
        runs.append(events)
    return runs


def _windows(events: list[dict]) -> dict:
    """Each stage of one run: first scheduled, last closed, attempts, completed."""
    scheduled, result = {}, {}
    for event in events:
        attrs = event.get("activityTaskScheduledEventAttributes")
        if attrs and attrs["activityType"]["name"] in STAGES:
            scheduled[event["eventId"]] = (attrs["activityType"]["name"], _time(event["eventTime"]))
        for kind in ("Completed", "Failed", "TimedOut", "Canceled"):
            closed = event.get(f"activityTask{kind}EventAttributes")
            if closed and closed["scheduledEventId"] in scheduled:
                stage, start = scheduled[closed["scheduledEventId"]]
                end = _time(event["eventTime"])
                entry = result.setdefault(stage, {"start": start, "end": end, "attempts": 0})
                entry["start"], entry["end"] = min(entry["start"], start), max(entry["end"], end)
                entry["attempts"] += 1
                entry["completed"] = entry.get("completed", False) or kind == "Completed"
    return result


def stages(db, aweme: str, generation: int) -> dict:
    """Each stage as it ran in the last run that completed it: seconds from its first
    scheduling to its close (retries included), attempts, and the model requests made
    in that window with their cost. A run that only re-read a stage's stored result
    does not replace the one that did the work."""
    with httpx.Client(base_url=TEMPORAL_UI, timeout=30) as http:
        runs = _runs(http, f"video-{aweme}-g{generation}")
    result = {}
    for events in runs:
        for stage, w in _windows(events).items():
            if not w["completed"]:
                continue
            calls, usd = db.execute(
                "SELECT count(*), coalesce(sum(cost_usd), 0) FROM localization.llm_calls "
                "WHERE aweme_id = %s AND generation = %s AND stage = %s "
                "AND created_at BETWEEN %s AND %s + interval '10 seconds'",
                (aweme, generation, stage, w["start"], w["end"]),
            ).fetchone()
            if calls == 0 and stage in result:
                continue  # this run re-read the stored result; keep the run that did the work
            result[stage] = {
                "seconds": round((w["end"] - w["start"]).total_seconds(), 1),
                "attempts": w["attempts"],
                "calls": calls,
                "usd": float(usd),
            }
    return result


def artifacts(store, aweme: str, generation: int) -> dict:
    from localization.contracts import SubtitleDocument

    prefix = f"work/{aweme}/g{generation}/r0/"
    subs = SubtitleDocument.model_validate_json(
        store.get_bytes(prefix + "subtitles.json", produced_by="detect_subtitles")
    )
    try:
        completed = json.loads(
            store.get_bytes(prefix + "transcript.subtitles.json", produced_by="complete_transcript")
        )
        lines = added_lines(completed)
    except Exception:  # noqa: BLE001 - no line was added
        lines = []
    return {"subs": subs, "lines": lines}


def compare(db, store, aweme: str, before: int, after: int) -> dict:
    old, new = artifacts(store, aweme, before), artifacts(store, aweme, after)
    a, b = old["subs"].region, new["subs"].region
    error = None
    if a and b:
        error = round(max(abs(a.y - b.y), abs(a.y + a.h - b.y - b.h)) / old["subs"].height, 4)
    for line in old["lines"]:
        line["found"] = any(same(line["text"], o["text"]) for o in new["lines"])
    for line in new["lines"]:
        line["in_before"] = any(same(line["text"], o["text"]) for o in old["lines"])
    return {
        "aweme_id": aweme,
        "before": before,
        "after": after,
        "duration_ms": new["subs"].duration_ms,
        "stages": {"before": stages(db, aweme, before), "after": stages(db, aweme, after)},
        "region": {
            "before": a and a.model_dump(),
            "after": b and b.model_dump(),
            "error": error,
            "frame_interval_ms": new["subs"].frame_interval_ms,
        },
        "lines": {"before": old["lines"], "after": new["lines"]},
    }


def main(argv: list[str]) -> int:
    import psycopg

    from localization.config import Settings
    from localization.storage.object_store import ObjectStore

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--db-host", default="localhost:5433")
    parser.add_argument("videos", nargs="+", help="aweme_id:earlier:later")
    args = parser.parse_args(argv)
    load_env()
    os.environ["S3_ENDPOINT"] = "http://127.0.0.1:9000"
    settings = Settings()
    store = ObjectStore.from_settings(settings)
    database = host_database_url(settings.loc_database_url.get_secret_value(), args.db_host)
    results = []
    with psycopg.connect(database) as db:
        for item in args.videos:
            aweme, before, after = item.split(":")
            results.append(compare(db, store, aweme, int(before), int(after)))
    target = args.out / "generations.json"
    target.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(json.dumps(results, ensure_ascii=False, indent=1))
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

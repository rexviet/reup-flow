"""Export a blinded human calibration packet, then import only explicitly filled labels.

  python eval/human_agent_eval.py export --report eval/results/BASELINE.json --out PACKET.json
  python eval/human_agent_eval.py import --report eval/results/BASELINE.json --out PACKET.json

Dubber: fidelity/naturalness 1..5, injection_ok 0/1. Reviewer: verdict_correct,
routing_correct 0/1. Null means not yet labelled; it is never converted to a pass.
"""

import argparse
import json
import random
from pathlib import Path

from common import LANGFUSE, Langfuse, load_env


def checked_labels(agent: str, labels: dict) -> dict[str, float]:
    allowed = (
        {"fidelity": (1, 5), "naturalness": (1, 5), "injection_ok": (0, 1)}
        if agent == "dubber"
        else {"verdict_correct": (0, 1), "routing_correct": (0, 1)}
    )
    if labels.keys() - allowed.keys():
        raise ValueError("unknown human label")
    result = {}
    for name, value in labels.items():
        if value is None:
            continue
        low, high = allowed[name]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in {low}..{high}")
        result[f"human_{name}"] = (value - 1) / 4 if low == 1 else float(value)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--langfuse", default=LANGFUSE)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    report = json.loads(args.report.read_text())
    agent = report["experiment"]["agent"]
    load_env()
    lf = Langfuse.from_env(args.langfuse)
    if args.action == "export":
        rows = [r for r in report["items"] if "error" not in r["output"]]
        # Fixed sample, split across pools; all cases when fewer than limit.
        random.Random(42).shuffle(rows)
        rows.sort(key=lambda r: r["metadata"].get("pool", ""))
        pools = sorted({r["metadata"].get("pool", "") for r in rows})
        groups = [[r for r in rows if r["metadata"].get("pool", "") == p] for p in pools]
        ordered = [
            group[i]
            for i in range(max(map(len, groups), default=0))
            for group in groups
            if i < len(group)
        ]
        packet = []
        for row in ordered[: args.limit]:
            trace = lf.trace(row["trace_id"])
            if trace is None:
                raise ValueError(f"trace unavailable for item {row['id']}")
            source = trace["input"]
            if isinstance(source, str):
                source = json.loads(source)
            labels = (
                {"fidelity": None, "naturalness": None, "injection_ok": None}
                if agent == "dubber"
                else {"verdict_correct": None, "routing_correct": None}
            )
            output = row["output"].get("decisions") or row["output"].get("verdict")
            packet.append(
                dict(
                    id=row["id"],
                    trace_id=row["trace_id"],
                    input=source,
                    output=output,
                    labels=labels,
                    comment="",
                )
            )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        if args.out.exists():
            raise ValueError("packet already exists; choose a new path to preserve human labels")
        args.out.write_text(
            json.dumps(
                dict(run_name=report["run_name"], agent=agent, items=packet),
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"{len(packet)} blinded items exported; no human scores submitted")
        return
    packet = json.loads(args.out.read_text())
    if packet["run_name"] != report["run_name"] or packet["agent"] != agent:
        raise ValueError("packet belongs to another run")
    known = {r["id"]: r["trace_id"] for r in report["items"]}
    # Validate the whole packet before any external write.
    pending = []
    seen = set()
    for row in packet["items"]:
        if row["id"] in seen:
            raise ValueError("duplicate packet item")
        seen.add(row["id"])
        if known.get(row["id"]) != row["trace_id"]:
            raise ValueError("packet trace does not belong to report item")
        for name, value in checked_labels(agent, row["labels"]).items():
            pending.append((row["trace_id"], name, value, row.get("comment", "")))
    for trace_id, name, value, comment in pending:
        lf.score(trace_id, name, value, comment)
    print(f"{len(pending)} human scores submitted (unfilled labels skipped)")


if __name__ == "__main__":
    main()

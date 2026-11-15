"""Reapply code checks to a saved experiment without calling an agent or a judge.

  python eval/rescore_agent_eval.py --dataset dubber-synth-v1 --report RESULTS.json

Inputs must still match their recorded traces; corrected expected labels are allowed.
Original experiment provenance is preserved, with a separate scorer fingerprint.
"""

import argparse
import json
from pathlib import Path

from agent_experiment import dubber_scores, reviewer_scores, source_fingerprint
from common import LANGFUSE, Langfuse, load_env


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--langfuse", default=LANGFUSE)
    args = parser.parse_args()
    load_env()
    lf = Langfuse.from_env(args.langfuse)
    report = json.loads(args.report.read_text())
    agent = report["experiment"]["agent"]
    items = {i["id"]: i for i in lf.items(args.dataset)}
    linked = lf.run_items(args.dataset, report["run_name"])
    pending = []
    for row in report["items"]:
        if linked.get(row["id"]) != row["trace_id"]:
            raise ValueError("report is not linked to this dataset run")
        recorded = lf.trace(row["trace_id"])
        inp = recorded["input"]
        inp = json.loads(inp) if isinstance(inp, str) else inp
        item = items[row["id"]]
        if item["input"] != inp:
            raise ValueError("input changed; run a new experiment instead of rescoring")
        if "error" in row["output"]:
            continue
        scores = (
            dubber_scores(inp, item["expectedOutput"], row["output"])
            if agent == "dubber"
            else reviewer_scores(item["expectedOutput"], row["output"])
        )
        pending.append((row, scores))
    for row, scores in pending:
        for score in scores:
            if score.value is not None:
                lf.score(row["trace_id"], score.name, score.value, score.comment)
            row["scores"][score.name] = score.value
    report["rescore_source_sha256"] = source_fingerprint()
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"{len(pending)} outputs rescored; zero model requests")


if __name__ == "__main__":
    main()

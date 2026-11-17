"""Builds the comparison page of `subtitle_sampling_gepa.py` from its `report.json`: one
self-contained HTML file (the report embedded as JSON, drawn by inline script), rebuilt
whenever a strategy is added.

    uv run python eval/subtitle_sampling_page.py --out ../../data/ocr-bench/sampling-v1
"""

import argparse
import json
import sys
from pathlib import Path

# The page around the data: `__DATA__` is replaced by the report, as JSON.
TEMPLATE = Path(__file__).with_suffix(".html")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="the study's directory")
    args = parser.parse_args(argv)
    report = json.loads((args.out / "report.json").read_text())
    # The changed code run by the real workflow, a generation against the one before it
    # (`subtitle_sampling_generations.py`), when it has been.
    runs = args.out / "generations.json"
    report["generations"] = json.loads(runs.read_text()) if runs.exists() else []
    payload = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    target = args.out / "report.html"
    target.write_text(TEMPLATE.read_text().replace("__DATA__", payload))
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

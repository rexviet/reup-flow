"""The eval scripts live in `eval/` and import each other as top-level modules, as they
do when run as `python eval/<script>.py`."""

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

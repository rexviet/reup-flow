"""Writes the JSON Schema of the contracts the renderer consumes.

python -m localization.contracts.export          # rewrite the file
python -m localization.contracts.export --check  # exit 1 if it is stale
"""

import json
import sys
from pathlib import Path

from localization.contracts.models import EditSpec

# <repo>/services/localization/src/localization/contracts/export.py
REPO_ROOT = Path(__file__).resolve().parents[5]
EDIT_SPEC_SCHEMA_PATH = REPO_ROOT / "contracts" / "edit-spec.schema.json"


def render_edit_spec_schema() -> str:
    return json.dumps(EditSpec.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    expected = render_edit_spec_schema()
    if "--check" in argv:
        actual = EDIT_SPEC_SCHEMA_PATH.read_text() if EDIT_SPEC_SCHEMA_PATH.exists() else None
        if actual != expected:
            print(f"{EDIT_SPEC_SCHEMA_PATH} is stale; run python -m localization.contracts.export")
            return 1
        return 0
    EDIT_SPEC_SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    EDIT_SPEC_SCHEMA_PATH.write_text(expected)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

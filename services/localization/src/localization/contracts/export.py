"""Writes the JSON Schema of the EditSpec, the contract the render consumes.

python -m localization.contracts.export          # rewrite the file
python -m localization.contracts.export --check  # exit 1 if it is stale
"""

import json
import sys
from pathlib import Path

from localization.contracts.models import EditSpec
from localization.contracts.review_project import ReviewHead, ReviewProjectManifest

# <repo>/services/localization/src/localization/contracts/export.py
REPO_ROOT = Path(__file__).resolve().parents[5]
EDIT_SPEC_SCHEMA_PATH = REPO_ROOT / "contracts" / "edit-spec.schema.json"


def render_edit_spec_schema() -> str:
    return json.dumps(EditSpec.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    schemas = {
        EDIT_SPEC_SCHEMA_PATH: render_edit_spec_schema(),
        REPO_ROOT / "contracts" / "review-project.schema.json": json.dumps(
            ReviewProjectManifest.model_json_schema(), indent=2, sort_keys=True
        ) + "\n",
        REPO_ROOT / "contracts" / "review-head.schema.json": json.dumps(
            ReviewHead.model_json_schema(), indent=2, sort_keys=True
        ) + "\n",
    }
    for path, expected in schemas.items():
        if "--check" in argv:
            if not path.exists() or path.read_text() != expected:
                print(f"{path} is stale; run python -m localization.contracts.export")
                return 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(expected)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

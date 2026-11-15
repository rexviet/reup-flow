"""Verify a review bundle using the worker identity, without editing the project."""

import argparse
import tempfile
from pathlib import Path

from localization.activities.review_project import materialize_project
from localization.config import load_settings
from localization.contracts.ids import validate_aweme_id
from localization.storage.object_store import ObjectStore, project_head_key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aweme_id")
    parser.add_argument("generation", type=int)
    args = parser.parse_args()
    validate_aweme_id(args.aweme_id)
    if args.generation < 0:
        parser.error("generation must be nonnegative")
    store = ObjectStore.from_settings(load_settings())
    key = project_head_key(args.aweme_id, args.generation)
    with tempfile.TemporaryDirectory(prefix="verify-review-") as temporary:
        manifest, _ = materialize_project(store, key, Path(temporary))
        if (manifest.aweme_id, manifest.generation) != (args.aweme_id, args.generation):
            raise ValueError("bundle belongs to a different run")
    print("Review HEAD, manifest, generation and all dependency checksums verified")


if __name__ == "__main__":
    main()

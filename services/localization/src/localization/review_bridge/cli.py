"""Trusted host launcher entry; emits the short-lived grant only on its private pipe."""

import argparse
import json

from localization.review_bridge.auth import Auth
from localization.review_bridge.repository import Repository
from localization.review_bridge.settings import BridgeSettings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["launch"])
    parser.add_argument("aweme")
    parser.add_argument("generation", type=int)
    args = parser.parse_args()
    settings = BridgeSettings()
    repository = Repository(settings.store(), settings.project_root)
    project, _ = repository.materialize(args.aweme, args.generation)
    grant = Auth(settings.runtime_root).issue(args.aweme, args.generation)
    print(
        json.dumps(
            {
                "aweme_id": args.aweme,
                "generation": args.generation,
                "revision": project.revision_id,
                "grant": grant,
            }
        )
    )


if __name__ == "__main__":
    main()

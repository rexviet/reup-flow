"""Which code a host worker runs, written into its heartbeat so the dashboard can tell an
outdated machine apart.

`REVIEW_PROTOCOL` is the version of the contract between the review project and the final
render: the review manifest (`contracts/review_project.py`), the files `export_revision` writes
and how the render worker reads them. Bump it whenever that contract changes in a way an older
worker cannot handle (a new manifest field, a new asset, a new render step). A live worker whose
protocol is lower than another live worker's stops claiming final renders, and the dashboard
asks for that machine to be updated.
"""

import functools
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("review_worker")

# 1: heartbeats without a protocol (every worker before this module existed).
# 2: manifests carry the collection's look (caption/cover colour, opacity, cover geometry).
REVIEW_PROTOCOL = 2

UNKNOWN = "unknown"
GIT_TIMEOUT_S = 5


@functools.cache
def code_version() -> str:
    """The short git commit of the checkout this code runs from; read once per process.
    "unknown" when git is missing, slow, or the code does not run from a checkout."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("code version unknown: %s", type(exc).__name__)
        return UNKNOWN
    commit = done.stdout.strip()
    if done.returncode != 0 or not commit:
        log.warning("code version unknown: git exited with %s", done.returncode)
        return UNKNOWN
    return commit

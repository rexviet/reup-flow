"""Reading and writing the stored `LocalizedScript`. Kept apart from the stages that
use it so the audio stages can read a script without reaching an agent."""

import re

from localization.contracts import LocalizedScript, LocalizedScriptRef
from localization.storage.object_store import ObjectStore, work_key

_VERSION_RE = re.compile(r"script\.v([0-9]+)\.json$")
# The Localizer writes the first version; the dubbing stage writes every later one.
LOCALIZER_VERSION = 1


def script_version(key: str) -> int:
    match = _VERSION_RE.search(key)
    if match is None:
        raise ValueError(f"{key} is not a script key")
    return int(match.group(1))


def producing_stage(key: str) -> str:
    """The stage to run again when this script is gone."""
    return "localize" if script_version(key) == LOCALIZER_VERSION else "dub_script"


def script_key(aweme_id: str, version: int, round: int = 0) -> str:
    """Versions grow through a run, whatever the round; the round only says which
    folder of the video's work a version was written into."""
    return work_key(aweme_id, round, f"script.v{version}.json")


def read_script(store: ObjectStore, key: str) -> LocalizedScript:
    return LocalizedScript.model_validate_json(
        store.get_bytes(key, produced_by=producing_stage(key))
    )


def write_script(store: ObjectStore, script: LocalizedScript, round: int = 0) -> LocalizedScriptRef:
    key = script_key(script.aweme_id, script.version, round)
    store.put_bytes(key, script.model_dump_json().encode(), content_type="application/json")
    return LocalizedScriptRef(script_key=key, version=script.version)

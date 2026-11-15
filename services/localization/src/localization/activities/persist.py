"""The only way workflow-owned data reaches the object store: workflow code is
deterministic and does no I/O, so it hands the data to this activity."""

import asyncio
import json
import re
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.storage.object_store import ObjectStore

# A JSON document of one video, under its own work/ folder: never a media artifact.
PERSIST_KEY_RE = re.compile(r"work/[0-9]{1,32}/.+\.json")


def put_json(store: ObjectStore, key: str, document: BaseModel | Any) -> None:
    body = (
        document.model_dump_json() if isinstance(document, BaseModel) else _dumps(document)
    ).encode()
    store.put_bytes(key, body, content_type="application/json")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class PersistActivity:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    @activity.defn(name="persist_json")
    async def persist_json(self, key: str, obj: Any) -> None:
        if not PERSIST_KEY_RE.fullmatch(key):
            raise ApplicationError(
                "persist_json writes only work/<video>/....json documents",
                type="InvalidPersistKey",
                non_retryable=True,
            )
        await asyncio.to_thread(put_json, self._store, key, obj)

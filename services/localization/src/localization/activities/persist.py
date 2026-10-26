"""The only way workflow-owned data reaches the object store: workflow code is
deterministic and does no I/O, so it hands the data to this activity."""

import asyncio
import json
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from localization.storage.object_store import ObjectStore

PERSIST_PREFIX = "work/"


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
        if not key.startswith(PERSIST_PREFIX):
            raise ApplicationError(
                f"persist_json writes only under {PERSIST_PREFIX}",
                type="InvalidPersistKey",
                non_retryable=True,
            )
        await asyncio.to_thread(put_json, self._store, key, obj)

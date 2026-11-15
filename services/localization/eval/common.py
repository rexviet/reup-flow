"""What the eval scripts share: the environment, Langfuse's public API, and run histories.

The scripts run on the host, next to the compose stack. They read the repo's `.env` for
keys and model names (never printing a key), and reach the services through the ports
the stack publishes on the host, which the flags can change:

  Langfuse     http://127.0.0.1:3100     (`--langfuse`)
  Temporal UI  http://127.0.0.1:8233     (`--temporal-ui`; its HTTP API serves histories)
  Postgres     localhost:5432            (the host part of LOC_DATABASE_URL is replaced)
"""

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values

REPO_ENV = Path(__file__).resolve().parents[3] / ".env"
LANGFUSE = "http://127.0.0.1:3100"
TEMPORAL_UI = "http://127.0.0.1:8233"
DB_HOST = "localhost:5432"


def load_env(path: Path = REPO_ENV, **overrides: str | None) -> None:
    """The repo's `.env` into the process environment, under any value already set, then
    `overrides` (a None override is skipped). Settings() reads the environment."""
    for key, value in dotenv_values(path).items():
        if value is not None:
            os.environ.setdefault(key, value)
    for key, value in overrides.items():
        if value is not None:
            os.environ[key.upper()] = value


def host_database_url(url: str, host: str = DB_HOST) -> str:
    """LOC_DATABASE_URL with its compose host name replaced by the one the host reaches."""
    parts = urlsplit(url)
    credentials = parts.netloc.rpartition("@")[0]
    netloc = f"{credentials}@{host}" if credentials else host
    return urlunsplit(parts._replace(netloc=netloc))


class Langfuse:
    """The parts of Langfuse's public API the eval needs: datasets, their items and
    runs, and scores."""

    def __init__(self, host: str, public_key: str, secret_key: str) -> None:
        self._http = httpx.Client(
            base_url=host.rstrip("/"), auth=(public_key, secret_key), timeout=30
        )

    @classmethod
    def from_env(cls, host: str) -> "Langfuse":
        return cls(host, os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 300:
            # The path and status only: a body may echo what was sent.
            raise RuntimeError(f"langfuse {method} {path}: HTTP {response.status_code}")
        return response.json() if response.content else None

    def ensure_dataset(self, name: str, description: str) -> None:
        response = self._http.get(f"/api/public/v2/datasets/{quote(name, safe='')}")
        if response.status_code == 404:
            self._call(
                "POST", "/api/public/v2/datasets", json={"name": name, "description": description}
            )
        elif response.status_code >= 300:
            raise RuntimeError(f"langfuse GET dataset: HTTP {response.status_code}")

    def upsert_item(
        self, dataset: str, item_id: str, input: Any, expected: Any, metadata: Any
    ) -> None:
        self._call(
            "POST",
            "/api/public/dataset-items",
            json={
                "datasetName": dataset,
                "id": item_id,
                "input": input,
                "expectedOutput": expected,
                "metadata": metadata,
            },
        )

    def items(self, dataset: str) -> Iterator[dict]:
        page = 1
        while True:
            body = self._call(
                "GET",
                "/api/public/dataset-items",
                params={"datasetName": dataset, "page": page, "limit": 50},
            )
            yield from body["data"]
            if page >= body["meta"]["totalPages"]:
                return
            page += 1

    def link_run_item(
        self, run: str, item_id: str, trace_id: str, description: str, metadata: Any
    ) -> None:
        self._call(
            "POST",
            "/api/public/dataset-run-items",
            json={
                "runName": run,
                "runDescription": description,
                "metadata": metadata,
                "datasetItemId": item_id,
                "traceId": trace_id,
            },
        )

    def score(
        self, trace_id: str, name: str, value: float, comment: str = "", *, data_type="NUMERIC"
    ) -> None:
        self._call(
            "POST",
            "/api/public/scores",
            json={
                "id": f"{trace_id}-{name}",
                "traceId": trace_id,
                "name": name,
                "value": value,
                "dataType": data_type,
                "comment": comment[:1000] or None,
            },
        )

    def run_items(self, dataset: str, run: str) -> dict[str, str]:
        """Item id -> trace id of the items a dataset run already holds; empty for a run
        that does not exist yet."""
        response = self._http.get(
            f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run, safe='')}"
        )
        if response.status_code == 404:
            return {}
        if response.status_code >= 300:
            raise RuntimeError(f"langfuse GET dataset run: HTTP {response.status_code}")
        return {i["datasetItemId"]: i["traceId"] for i in response.json()["datasetRunItems"]}

    def delete_item(self, item_id: str) -> None:
        self._call("DELETE", f"/api/public/dataset-items/{quote(item_id, safe='')}")

    def trace(self, trace_id: str) -> dict | None:
        response = self._http.get(f"/api/public/traces/{trace_id}")
        return response.json() if response.status_code == 200 else None


# --- run histories ------------------------------------------------------------------------


def payloads(field: dict | None) -> list[Any]:
    """The JSON values of a history field's payloads (the service's payloads are plain
    JSON; the codec only checks their size)."""
    return [json.loads(base64.b64decode(p["data"])) for p in (field or {}).get("payloads", [])]


class History:
    """A workflow's history as the Temporal UI's HTTP API serves it."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self._by_id = {int(e["eventId"]): e for e in events}

    @classmethod
    def fetch(cls, ui: str, workflow_id: str, namespace: str = "default") -> "History":
        events: list[dict] = []
        token = None
        with httpx.Client(base_url=ui.rstrip("/"), timeout=30) as http:
            while True:
                params = {"next_page_token": token} if token else {}
                path = f"/api/v1/namespaces/{namespace}/workflows/{quote(workflow_id, safe='')}"
                response = http.get(f"{path}/history", params=params)
                response.raise_for_status()
                body = response.json()
                events += body["history"]["events"]
                token = body.get("nextPageToken")
                if not token:
                    return cls(events)

    def activities(self, name: str) -> list[tuple[list[Any], Any]]:
        """(input, result) of each completed call of activity `name`, in order."""
        scheduled = {
            int(e["eventId"]): payloads(e["activityTaskScheduledEventAttributes"].get("input"))
            for e in self.events
            if (a := e.get("activityTaskScheduledEventAttributes"))
            and a["activityType"]["name"] == name
        }
        calls = []
        for e in self.events:
            done = e.get("activityTaskCompletedEventAttributes")
            if done and int(done["scheduledEventId"]) in scheduled:
                result = payloads(done.get("result"))
                calls.append(
                    (scheduled[int(done["scheduledEventId"])], result[0] if result else None)
                )
        return calls

    def result(self) -> Any:
        for e in self.events:
            done = e.get("workflowExecutionCompletedEventAttributes")
            if done:
                values = payloads(done.get("result"))
                return values[0] if values else None
        return None

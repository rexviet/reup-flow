"""One row in `localization.host_workers` per loop: liveness, disk and VoiceStudio."""

import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb

from localization.review_worker.config import DEFAULT_NAME as WORKER_NAME

WELL_KNOWN = "/.well-known/voicestudio-speech"

UPSERT_SQL = """
INSERT INTO localization.host_workers (name, seen_at, info) VALUES (%s, now(), %s)
ON CONFLICT (name) DO UPDATE SET seen_at = now(), info = EXCLUDED.info
"""


def probe_voicestudio(base_url: str | None, *, client: httpx.Client | None = None) -> dict:
    checked_at = datetime.now(UTC).isoformat()
    if not base_url:
        return {"ok": False, "latency_ms": None, "checked_at": checked_at}
    started = time.monotonic()
    try:
        owned = client is None
        http = client or httpx.Client(timeout=2.0, trust_env=False)
        try:
            response = http.get(base_url.rstrip("/") + WELL_KNOWN)
        finally:
            if owned:
                http.close()
        ok = response.is_success
    except httpx.HTTPError:
        ok = False
    return {
        "ok": ok,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "checked_at": checked_at,
    }


def build_info(
    *, disk_free: int, disk_total: int, voicestudio: dict, current: dict | None,
    paused_reason: str | None,
) -> dict:  # fmt: skip
    info = {
        "disk_free_bytes": disk_free,
        "disk_total_bytes": disk_total,
        "voicestudio": voicestudio,
        "current": current,
    }
    if paused_reason:
        info["paused_reason"] = paused_reason
    return info


def beat(conn, info: dict, name: str = WORKER_NAME) -> None:
    conn.execute(UPSERT_SQL, (name, Jsonb(info)))


def data_disk(data_root: Path) -> Path:
    """The drive holding `data/`, which exists even before the first review project."""
    return data_root.parent if data_root.parent.exists() else data_root

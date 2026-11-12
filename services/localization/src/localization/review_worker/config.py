"""Worker settings. They come from the compose environment of `worker-agents` (through
scripts/review-worker.mjs), not from BridgeSettings, which is for the container."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from localization.config import ConfigError


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    data_root: Path
    work_root: Path
    render_workers: int = 2
    min_free_gb: float = 5.0
    node: str = "node"
    hyperframes_bin: str | None = None
    studio_script: Path | None = None
    voicestudio_url: str | None = None
    # Hard limit is shorter than the lease so a stuck render is killed before the row
    # becomes claimable again.
    render_timeout_s: int = 30 * 60
    lease_minutes: int = 45
    renew_every_s: int = 5 * 60
    shutdown_grace_s: int = 60
    max_attempts: int = 3

    @classmethod
    def from_environ(cls, env: Mapping[str, str] | None = None) -> "WorkerConfig":
        from localization.config import load_settings

        env = os.environ if env is None else env
        settings = load_settings()
        if settings.loc_database_url is None:
            raise ConfigError("LOC_DATABASE_URL is required")
        root = env.get("REVIEW_WORKER_DATA_ROOT")
        if not root or not Path(root).is_absolute():
            raise ConfigError("REVIEW_WORKER_DATA_ROOT must be an absolute path")
        data_root = Path(root)
        try:
            workers = int(env.get("FINAL_RENDER_WORKERS", "2"))
            min_free = float(env.get("REVIEW_WORKER_MIN_FREE_GB", "5"))
        except ValueError:
            raise ConfigError(
                "FINAL_RENDER_WORKERS / REVIEW_WORKER_MIN_FREE_GB are invalid"
            ) from None
        if workers < 1:
            raise ConfigError("FINAL_RENDER_WORKERS must be at least 1")
        studio = env.get("REVIEW_WORKER_STUDIO_SCRIPT")
        return cls(
            database_url=settings.loc_database_url.get_secret_value(),
            data_root=data_root,
            work_root=data_root.parent / "review-render",
            render_workers=workers,
            min_free_gb=min_free,
            node=env.get("REVIEW_WORKER_NODE", "node"),
            hyperframes_bin=env.get("HYPERFRAMES_BIN"),
            studio_script=Path(studio) if studio else None,
            voicestudio_url=env.get("VOICESTUDIO_URL"),
        )

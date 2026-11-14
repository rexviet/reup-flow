"""Worker settings. On the Mac they come from the compose environment of `worker-agents`
(through scripts/review-worker.mjs); on a Windows render machine from `.render-worker.env`
(through scripts/render-worker.ps1). Not from BridgeSettings, which is for the container."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from localization.config import ConfigError

# `render`: claim and render approved revisions. `studio`: Studio open requests, review scans,
# the poster backfill and the video thumbnails, which need this machine's `data/review` and
# Studio (thumbnails only need storage, but run beside the other background work).
ROLES = frozenset({"render", "studio"})
DEFAULT_NAME = "review-worker"  # the name the dashboard reads
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
TRUE, FALSE = {"1", "true", "yes", "on"}, {"", "0", "false", "no", "off"}


def parse_roles(raw: str | None) -> frozenset[str]:
    """`all` (default) or a comma-separated subset of `render`, `studio`."""
    words = {word.strip().lower() for word in (raw or "all").split(",") if word.strip()}
    if words == {"all"}:
        return ROLES
    if not words or not words <= ROLES:
        raise ConfigError("REVIEW_WORKER_ROLES must be `all` or a list of: render, studio")
    return frozenset(words)


def parse_flag(name: str, raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    data_root: Path
    work_root: Path
    roles: frozenset[str] = ROLES
    name: str = DEFAULT_NAME
    render_workers: int = 2
    # Adds HyperFrames' `--gpu` (hardware encoding) to the render command; off by default.
    render_gpu: bool = False
    min_free_gb: float = 5.0
    node: str = "node"
    hyperframes_bin: str | None = None
    studio_script: Path | None = None
    voicestudio_url: str | None = None
    # Hard limit is shorter than the lease so a stuck render is killed before the row
    # becomes claimable again.
    render_timeout_s: int = 60 * 60
    lease_minutes: int = 75
    renew_every_s: int = 5 * 60
    shutdown_grace_s: int = 60
    max_attempts: int = 3
    # Save-to-folder writes only under this directory; None leaves those requests pending.
    export_root: Path | None = None
    # Absolute path of ffmpeg for posters and thumbnails; None looks it up on PATH.
    ffmpeg_bin: str | None = None

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
        name = env.get("REVIEW_WORKER_NAME", DEFAULT_NAME).strip()
        export = env.get("REVIEW_WORKER_EXPORT_ROOT", "").strip()
        # Not checked for existence: a drive mounted after boot must not stop the heartbeat
        # and Studio requests; until it is there, each export fails with a sentence instead.
        if export and not Path(export).is_absolute():
            raise ConfigError("REVIEW_WORKER_EXPORT_ROOT must be an absolute path")
        ffmpeg = env.get("FFMPEG_BIN", "").strip()
        if ffmpeg and not Path(ffmpeg).is_absolute():
            raise ConfigError("FFMPEG_BIN must be an absolute path")
        if not NAME.fullmatch(name):
            raise ConfigError("REVIEW_WORKER_NAME must be lowercase letters, digits and dashes")
        return cls(
            database_url=settings.loc_database_url.get_secret_value(),
            data_root=data_root,
            work_root=data_root.parent / "review-render",
            roles=parse_roles(env.get("REVIEW_WORKER_ROLES")),
            name=name,
            render_workers=workers,
            render_gpu=parse_flag("FINAL_RENDER_GPU", env.get("FINAL_RENDER_GPU")),
            min_free_gb=min_free,
            node=env.get("REVIEW_WORKER_NODE", "node"),
            hyperframes_bin=env.get("HYPERFRAMES_BIN"),
            studio_script=Path(studio) if studio else None,
            voicestudio_url=env.get("VOICESTUDIO_URL"),
            export_root=Path(export) if export else None,
            ffmpeg_bin=ffmpeg or None,
        )

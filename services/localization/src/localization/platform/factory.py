"""Which implementation runs: `REVIEW_PLATFORM` is `auto` (default, from `os.name`), `posix`
or `windows`. Anything else stops the worker at start rather than guessing."""

import functools
import os
from collections.abc import Mapping

from localization.config import ConfigError
from localization.platform.base import Platform

CHOICES = ("auto", "posix", "windows")


def native(os_name: str = os.name) -> str:
    return "windows" if os_name == "nt" else "posix"


def build(name: str) -> Platform:
    if name == "windows":
        from localization.platform.windows import WindowsFileLock, WindowsProcessControl

        return Platform("windows", WindowsFileLock(), WindowsProcessControl())
    from localization.platform.posix import PosixFileLock, PosixProcessControl

    return Platform("posix", PosixFileLock(), PosixProcessControl())


def get_platform(env: Mapping[str, str] | None = None, *, os_name: str = os.name) -> Platform:
    env = os.environ if env is None else env
    wanted = env.get("REVIEW_PLATFORM", "auto").strip().lower() or "auto"
    if wanted not in CHOICES:
        raise ConfigError(f"REVIEW_PLATFORM must be one of {', '.join(CHOICES)}")
    here = native(os_name)
    name = here if wanted == "auto" else wanted
    # Only for tests: the other OS's implementation does not work here.
    if name != here and env.get("REVIEW_PLATFORM_FORCE") != "1":
        raise ConfigError(
            f"REVIEW_PLATFORM={name} on a {here} machine needs REVIEW_PLATFORM_FORCE=1"
        )
    return build(name)


@functools.cache
def current() -> Platform:
    """The platform of this process, chosen once from its environment."""
    return get_platform()

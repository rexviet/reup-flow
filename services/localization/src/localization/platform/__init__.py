"""OS-specific parts of the review worker (file lock, renderer process tree), behind two
narrow interfaces. Claims, leases, retries, uploads and exports are the same everywhere and
stay out of here."""

from localization.platform.base import FileLock, NotSupported, Platform, ProcessControl
from localization.platform.factory import current, get_platform

__all__ = ["FileLock", "NotSupported", "Platform", "ProcessControl", "current", "get_platform"]

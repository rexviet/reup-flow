"""Provider failure classification.

Messages are built from the provider name and the HTTP status only. A response
body or an exception string can echo a request header, and the Authorization
header is the one thing that must never reach history, a span or a log line.
"""

from temporalio.exceptions import ApplicationError

TRANSIENT_STATUSES = frozenset({408, 429})


class ProviderError(Exception):
    pass


class TransientProviderError(ProviderError):
    """Worth retrying: 408, 429, 5xx, timeouts, connection failures."""


class PermanentProviderError(ProviderError):
    """Retrying cannot help: other 4xx, `no_audio_track`, `no_video_stream`."""


def classify_status(provider: str, status: int) -> ProviderError:
    if status in TRANSIENT_STATUSES or 500 <= status <= 599:
        return TransientProviderError(f"{provider}: HTTP {status}")
    return PermanentProviderError(f"{provider}: HTTP {status}")


def to_application_error(exc: ProviderError) -> ApplicationError:
    """What an activity raises so Temporal retries (or stops) accordingly."""
    return ApplicationError(
        str(exc),
        type=type(exc).__name__,
        non_retryable=isinstance(exc, PermanentProviderError),
    )

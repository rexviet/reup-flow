from collections.abc import Iterator
from contextlib import contextmanager

from localization.providers.errors import ProviderError, to_application_error


@contextmanager
def provider_errors() -> Iterator[None]:
    """Provider failures leave an activity as ApplicationErrors, so Temporal
    retries a transient one and stops on a permanent one."""
    try:
        yield
    except ProviderError as exc:
        raise to_application_error(exc) from None

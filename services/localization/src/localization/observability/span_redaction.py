"""Keeps credentials out of exported spans.

With content capture on, a span carries text the process did not write: a prompt, a tool
result, the message of an exception. A provider or a gateway that echoes the request
headers in an error body would put the API key in that message (an echoed key reaches a
pydantic-ai span verbatim; the canary test shows it). So every span is checked on its way
out: each credential the settings hold, and anything sent as an `Authorization` value, is
replaced before the span leaves the process.
"""

import re
from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

REDACTED = "[REDACTED]"
# Shorter values are not worth replacing: they would match ordinary words.
MIN_SECRET_LENGTH = 8
_AUTH_VALUE = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}")


class RedactingExporter(SpanExporter):
    def __init__(self, inner: SpanExporter, secrets: Sequence[str]) -> None:
        self._inner = inner
        # Longest first, so a secret that contains another is replaced whole.
        self._secrets = sorted(
            {s for s in secrets if len(s) >= MIN_SECRET_LENGTH}, key=len, reverse=True
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([self._clean(span) for span in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return _AUTH_VALUE.sub(lambda m: f"{m[1]} {REDACTED}", text)

    def _value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, tuple | list):
            return tuple(self._value(v) for v in value)
        return value

    def _attributes(self, attributes: Any) -> dict[str, Any]:
        return {key: self._value(value) for key, value in (attributes or {}).items()}

    def _clean(self, span: ReadableSpan) -> ReadableSpan:
        if not self._dirty(span):
            return span
        return ReadableSpan(
            name=self.redact(span.name),
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=self._attributes(span.attributes),
            events=[
                Event(e.name, self._attributes(e.attributes), e.timestamp) for e in span.events
            ],
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def _dirty(self, span: ReadableSpan) -> bool:
        texts = [span.name, *_strings(span.attributes)]
        for event in span.events:
            texts += _strings(event.attributes)
        return any(self.redact(t) != t for t in texts)


def _strings(attributes: Any) -> list[str]:
    found: list[str] = []
    for value in (attributes or {}).values():
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, tuple | list):
            found += [v for v in value if isinstance(v, str)]
    return found

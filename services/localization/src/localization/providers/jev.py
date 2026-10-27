"""Jev, TypeSafe's typed decision model, reached through OpenRouter's `/systemone`.

One `system_one` call takes a JSON state and named Noul questions and answers each with a
probability; thresholds are the caller's. The SDK's own retries are off: Temporal owns
retries. Errors are classified the way the other providers' are, so the workflow's retry
policy needs nothing new:
- 400/422, a body the SDK cannot parse, answers that break the questions: permanent
- 401/403: permanent, and never with the response body (an auth body may echo a credential)
- 429: transient, Retry-After honoured; a wait over `MAX_RETRY_AFTER` is permanent
- 5xx, timeouts, connection failures, 408: transient
- other 4xx: permanent

Only text goes out (the state the caller built), never a frame or the video.
"""

import asyncio
import contextvars
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import httpx2
from opentelemetry import trace
from temporalio.exceptions import ApplicationError
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Noul,
    NoulAnswer,
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAuthenticationError,
    TypeSafeBadRequestError,
    TypeSafeError,
    TypeSafeInternalServerError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
    TypeSafeUnprocessableEntityError,
)

from localization.config import ConfigError, Settings
from localization.observability.llm_usage import LlmUsage, usage_attributes

NO_SDK_RETRY = RetryPolicy(max_retries=0)
# Per attempt, below the review activity's start_to_close.
DEFAULT_TIMEOUT_SECONDS = 45.0
# A longer Retry-After would hold a review for longer than a rerun is worth.
MAX_RETRY_AFTER = timedelta(seconds=60)

PERMANENT = "PermanentProviderError"
TRANSIENT = "TransientProviderError"

log = logging.getLogger(__name__)
_tracer = trace.get_tracer(__name__)


@dataclass(frozen=True)
class JevResult:
    model: str
    # Question name -> probability that the question's statement is true.
    answers: dict[str, float]
    input_tokens: int
    output_tokens: int
    latency_ms: int


# Called once per answered request, before the answers are checked: a request that came
# back unusable was still billed.
UsageSink = Callable[[LlmUsage], Awaitable[None]]


# What OpenRouter puts in the body that the SDK's response model drops: the generation id
# and the cost in USD. The SDK keeps only the model, the answers and the token counts, so
# the response hook below copies these two into the dict of the call being made (a
# ContextVar, because one client serves several reviews at once).
_raw: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "jev_raw", default=None
)


async def _capture(response: httpx2.Response) -> None:
    target = _raw.get()
    if target is None or response.status_code != 200:
        return
    try:
        await response.aread()
        body = response.json()
        usage = body.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        target.update(
            id=body.get("id"),
            model=body.get("model"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost=usage.get("cost"),
            provider=body.get("provider"),
        )
    except Exception:  # noqa: BLE001 - observing a response must never fail it
        log.warning("could not read the generation id and cost of a jev response")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _count(value: object) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def _usage_of(raw: dict[str, object]) -> LlmUsage:
    """What the response body said it cost: ids and numbers only. A missing generation
    id gets one of our own, so the row still counts once."""
    generation_id, model, provider = raw.get("id"), raw.get("model"), raw.get("provider")
    return LlmUsage(
        generation_id=generation_id
        if isinstance(generation_id, str) and generation_id
        else f"jev-{uuid4().hex}",
        model=model if isinstance(model, str) and model else None,
        prompt_tokens=_count(raw.get("input_tokens")),
        completion_tokens=_count(raw.get("output_tokens")),
        # What OpenRouter charged; None (not a guess) when the body had none.
        cost_usd=_number(raw.get("cost")),
        provider=provider if isinstance(provider, str) and provider else None,
    )


def make_client(
    api_key: str,
    *,
    base_url: str,
    model: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> AsyncTypeSafeClient:
    """Explicit key, host and model, so TYPESAFE_* variables in the environment cannot
    redirect the key or swap the model behind Settings."""
    return AsyncTypeSafeClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        retry=NO_SDK_RETRY,
        http_client=httpx2.AsyncClient(
            transport=transport,
            timeout=httpx2.Timeout(timeout, connect=5.0),
            event_hooks={"response": [_capture]},
        ),
    )


def _error(msg: str, etype: str, delay: timedelta | None = None) -> ApplicationError:
    return ApplicationError(
        msg, type=etype, non_retryable=etype == PERMANENT, next_retry_delay=delay
    )


def classify(e: TypeSafeError, secret: str | None = None) -> ApplicationError:
    """The SDK's exception as the error the activity raises. The message lands in
    history and logs: no headers, the key scrubbed, no body for an auth failure."""
    if isinstance(e, TypeSafeAuthenticationError | TypeSafePermissionDeniedError):
        return _error(f"jev: HTTP {e.status} (request_id={e.request_id})", PERMANENT)
    msg = f"jev: {e}"
    if secret:
        msg = msg.replace(secret, "***")
    if isinstance(
        e,
        TypeSafeAPIResponseValidationError
        | TypeSafeUnprocessableEntityError
        | TypeSafeBadRequestError,
    ):
        return _error(msg, PERMANENT)
    if isinstance(e, TypeSafeRateLimitError):
        ms = e.retry_after_ms
        if ms and ms > MAX_RETRY_AFTER.total_seconds() * 1000:  # before timedelta(): 1e18
            return _error(f"{msg} (Retry-After {ms / 1000:.0f}s)", PERMANENT)
        return _error(msg, TRANSIENT, timedelta(milliseconds=ms) if ms else None)
    if isinstance(e, TypeSafeInternalServerError | TypeSafeAPIConnectionError):
        return _error(msg, TRANSIENT)  # a timeout is a connection error
    if isinstance(e, TypeSafeAPIError):
        return _error(msg, TRANSIENT if e.status == 408 else PERMANENT)
    return _error(msg, PERMANENT)  # the SDK refused the request itself


def check_answers(names: list[str], answers: Mapping[str, object]) -> list[str]:
    """What is wrong with Jev's answers to `names` (all Noul): empty means usable."""
    problems = []
    for name in names:
        answer = answers.get(name)
        if not isinstance(answer, NoulAnswer):
            problems.append(f"{name}: expected a noul answer")
            continue
        p = answer.noul
        if p is None or not math.isfinite(p) or not 0.0 <= p <= 1.0:
            problems.append(f"{name}: probability {p!r} outside [0, 1]")
    return problems


class Jev:
    """One SDK client for the worker's process. The key never leaves it."""

    def __init__(
        self,
        client: AsyncTypeSafeClient,
        *,
        model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        secret: str | None = None,
    ) -> None:
        self._client, self._model, self._timeout, self._secret = client, model, timeout, secret

    async def ask(
        self,
        state: dict[str, object],
        questions: Mapping[str, Noul],
        on_usage: UsageSink | None = None,
    ) -> JevResult:
        with _tracer.start_as_current_span("jev system_one") as span:
            raw: dict[str, object] = {}
            _raw.set(raw)
            t0 = time.monotonic()
            try:
                # httpx timeouts are per phase; this bounds the whole attempt.
                async with asyncio.timeout(self._timeout):
                    reply = await self._client.system_one(
                        state=state,
                        questions=dict(questions),
                        model=self._model,
                        retry=NO_SDK_RETRY,
                    )
            except TimeoutError:
                raise _error(f"jev: no answer within {self._timeout:.0f}s", TRANSIENT) from None
            except TypeSafeError as e:
                if raw and on_usage is not None:
                    # A 200 the SDK could not parse was still billed.
                    await on_usage(_usage_of(raw))
                raise classify(e, self._secret) from None  # the SDK error carries headers
            latency_ms = int((time.monotonic() - t0) * 1000)
            usage = _usage_of(raw)
            # Counts and the model only: never the state or the questions.
            span.set_attributes(usage_attributes(usage))
            if on_usage is not None:
                await on_usage(usage)
            problems = check_answers(list(questions), reply.answers)
            if problems:
                raise _error(f"jev answers invalid: {'; '.join(problems[:5])}", PERMANENT)
            return JevResult(
                model=reply.model,
                answers={
                    name: answer.noul
                    for name, answer in reply.answers.items()
                    if name in questions
                    and isinstance(answer, NoulAnswer)
                    and answer.noul is not None
                },
                input_tokens=reply.usage.input_tokens,
                output_tokens=reply.usage.output_tokens,
                latency_ms=latency_ms,
            )


def jev_for(settings: Settings) -> Jev:
    """Jev behind the OpenRouter base URL and key of the settings."""
    if settings.openrouter_api_key is None:
        raise ConfigError("OPENROUTER_API_KEY is not set")
    key = settings.openrouter_api_key.get_secret_value()
    client = make_client(key, base_url=settings.jev_base_url, model=settings.reviewer_jev_model)
    return Jev(client, model=settings.reviewer_jev_model, secret=key)

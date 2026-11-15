"""The scores a finished run leaves on its own Langfuse trace.

`publish` is the last activity of every run, inside the run's trace (the Temporal
interceptor carries the trace from the workflow into each activity), so the trace id it
reads from the current span is the run's. It sends:

  from the Reviewer   fidelity, timing, visual (when a review was made)
  measured            overflow_rate, mean_stretch, residual_han_cues, review_rounds
  yes or no           needs_human, safety_flag

`overflow_rate` is the share of dubbed lines still longer than their slot at the fastest
stretch, `mean_stretch` the mean speed-up of the dubbed lines, `residual_han_cues` the
number of final lines that still hold a Han character (each becomes a caption), and
`safety_flag` says a line carried a contact its source lacked (the Localizer flagged it,
or a Dubber rewrite was refused for it).

Each score's id is made from the trace and the score's name, so sending the same scores
again (a retried activity) overwrites them instead of adding a second set. Sending is
best effort: a run is never failed, or retried, for the sake of its scores.
"""

import base64
import logging
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import httpx
from opentelemetry import trace

from localization.config import Settings
from localization.contracts import DubSegmentsDocument, LocalizedScript, ReviewVerdict

log = logging.getLogger(__name__)

DataType = Literal["NUMERIC", "BOOLEAN"]
_SCORES_PATH = "/api/public/scores"
SEND_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Score:
    name: str
    value: float
    data_type: DataType = "NUMERIC"


def has_han(text: str) -> bool:
    return any(unicodedata.name(c, "").startswith("CJK UNIFIED IDEOGRAPH") for c in text)


def review_scores(verdict: ReviewVerdict | None) -> list[Score]:
    if verdict is None:
        return []
    s = verdict.scores
    return [Score("fidelity", s.fidelity), Score("timing", s.timing), Score("visual", s.visual)]


def dub_scores(segments: DubSegmentsDocument | None) -> list[Score]:
    if segments is None or not segments.segments:
        return []
    items = segments.segments
    return [
        Score("overflow_rate", sum(s.overflow_unresolved for s in items) / len(items)),
        Score("mean_stretch", sum(s.stretch_ratio for s in items) / len(items)),
    ]


def script_scores(script: LocalizedScript | None) -> list[Score]:
    if script is None:
        return []
    return [Score("residual_han_cues", sum(has_han(s.text_vi) for s in script.segments))]


def run_scores(
    *,
    verdict: ReviewVerdict | None,
    segments: DubSegmentsDocument | None,
    script: LocalizedScript | None,
    review_rounds: int,
    needs_human: bool,
    safety_flag: bool,
) -> list[Score]:
    """Every score the run has. A part the run never reached (no review, no dub) has no
    score rather than a made-up one."""
    return [
        *review_scores(verdict),
        *dub_scores(segments),
        *script_scores(script),
        Score("review_rounds", review_rounds),
        Score("needs_human", float(needs_human), "BOOLEAN"),
        Score("safety_flag", float(safety_flag), "BOOLEAN"),
    ]


def current_trace_id() -> str | None:
    """The run's trace, as Langfuse names it (32 hex digits), or None.

    None outside a recorded trace, and also when the current span is a root: a workflow
    started without a trace (from the UI, or by a client without the interceptor) gives
    each activity attempt a trace of its own, so scores sent there would miss the run
    and a retried attempt would write a second set."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid or getattr(span, "parent", None) is None:
        return None
    return format(context.trace_id, "032x")


class LangfuseScores:
    """Writes scores to a trace through Langfuse's public API."""

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout: float = SEND_TIMEOUT_SECONDS,
    ) -> None:
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._url = f"{host.rstrip('/')}{_SCORES_PATH}"
        self._headers = {"Authorization": f"Basic {token}"}
        self._http = http
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> "LangfuseScores | None":
        public, secret = settings.langfuse_public_key, settings.langfuse_secret_key
        if not (settings.langfuse_host and public and secret):
            return None
        return cls(settings.langfuse_host, public.get_secret_value(), secret.get_secret_value())

    @staticmethod
    def score_id(trace_id: str, name: str) -> str:
        return f"{trace_id}-{name}"

    async def send(self, trace_id: str, scores: Sequence[Score]) -> int:
        """Sends each score; returns how many Langfuse accepted. Never raises.

        A score names its trace only: Langfuse refuses one that names a session too
        (HTTP 400). The trace already belongs to the video's session."""
        accepted = 0
        http = self._http or httpx.AsyncClient(timeout=self._timeout)
        try:
            for score in scores:
                body = {
                    "id": self.score_id(trace_id, score.name),
                    "traceId": trace_id,
                    "name": score.name,
                    "value": score.value,
                    "dataType": score.data_type,
                }
                try:
                    response = await http.post(
                        self._url, json=body, headers=self._headers, timeout=self._timeout
                    )
                except httpx.HTTPError as exc:
                    # The type only: an error message may carry the URL.
                    log.warning("score %s not sent: %s", score.name, type(exc).__name__)
                    continue
                if 200 <= response.status_code < 300:
                    accepted += 1
                else:
                    log.warning("score %s refused: HTTP %d", score.name, response.status_code)
        finally:
            if self._http is None:
                await http.aclose()
        return accepted

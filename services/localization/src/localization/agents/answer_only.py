"""One-request agents answer without reasoning, within a token cap sized to the script.

The Localizer and the Dubber of the one-request mode return a short list of lines. A
reasoning model served by some OpenRouter providers thinks for thousands of tokens first:
the same request was answered in 5 s with 47 tokens by one provider and in 40 s with 2,600
tokens (2,000 of them reasoning) by another, and real runs spent 8.8k and 13.6k completion
tokens ($0.02 to $0.04, 163 to 242 s) on a 4-line script. Reasoning length depends on which
provider serves the call, not on the video, so the request itself says it wants the answer
only (`reasoning.enabled=false`, as the OCR provider's does) and a cap that a runaway
cannot pass.

Two halves, because the setting that switches it back is read by the worker and workflow
code must not read the environment:

- the agents (workflow side) always declare the settings, sized from their line count;
- `AnswerOnlyModel` (worker side) sends them as declared when the role's reasoning is
  `off`, and removes them when it is `default`, so the request is the plain one it was.

Only requests that carry the declared settings are touched: the tool-calling agents and
every other caller of the same model pass through unchanged.
"""

from typing import Any

from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

ANSWER_ONLY_BODY: dict[str, Any] = {"reasoning": {"enabled": False}}

# An answer is `{"id": n, "text_vi": "..."}` for each line. Answered without reasoning, a
# 3 to 4 line script took 37 to 101 tokens; a line of speech is at most about 120
# Vietnamese syllables (a long segment), about 300 tokens. The cap is a base plus that
# allowance for every line, so it grows with the script and a legitimate answer is far
# under it at any size: 150 lines (the most a run accepts) are allowed 46,500 tokens, held
# to the ceiling below, against the 9 to 15k such an answer takes. A 4-line script is
# allowed 2,700, below the 8.8k and 13.6k runaways.
BASE_TOKENS = 1500
LINE_TOKENS = 300
# Some providers refuse a request that asks for more output than their model allows.
CEILING_TOKENS = 32768


def answer_cap(lines: int) -> int:
    return min(CEILING_TOKENS, BASE_TOKENS + LINE_TOKENS * max(lines, 1))


def answer_only_settings(lines: int) -> ModelSettings:
    return {"max_tokens": answer_cap(lines), "extra_body": dict(ANSWER_ONLY_BODY)}


def is_answer_only(settings: ModelSettings | None) -> bool:
    return bool(settings) and (settings or {}).get("extra_body") == ANSWER_ONLY_BODY


class AnswerOnlyModel(WrapperModel):
    """The worker's model for a role, applying the role's reasoning setting.

    An answer cut off at the cap is raised as a model API error: the activity turns it
    into a retryable error. It is retryable because the cap is generous for a legitimate
    answer, so hitting it means this attempt ran away (a provider that ignored the
    setting, or a loop), and the next attempt is usually served differently; Temporal's
    attempts bound the cost, and the run fails if every one is cut off. It is not parsed:
    a list of lines cut in the middle would read as a short, valid answer.
    """

    def __init__(self, wrapped: Any, *, reasoning_off: bool) -> None:
        super().__init__(wrapped)
        self._reasoning_off = reasoning_off

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        if not is_answer_only(model_settings):
            return await self.wrapped.request(messages, model_settings, model_request_parameters)
        if not self._reasoning_off:
            plain = {
                k: v
                for k, v in (model_settings or {}).items()
                if k not in ("extra_body", "max_tokens")
            }
            return await self.wrapped.request(messages, plain, model_request_parameters)  # type: ignore[arg-type]
        response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        if response.finish_reason == "length":
            raise ModelAPIError(
                self.model_name,
                f"the answer was cut off at {(model_settings or {}).get('max_tokens')} tokens",
            )
        return response

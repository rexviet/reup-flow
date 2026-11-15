"""One-request agents ask for the answer only, within a cap that fits the script; the
worker's switch puts the plain request back; an answer cut off at the cap is an error
that Temporal retries. The HTTP test sends the real request through the production model
to an endpoint that records its body, as OpenRouter's chat completions would receive it."""

import json
from collections.abc import Iterator

import pytest
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.durable_exec.temporal._model_errors import model_errors_as_application_errors
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage
from temporalio.exceptions import ApplicationError

from localization.agents import answer_only as ao
from localization.agents import localizer_single
from localization.agents.localizer import LocalizerDeps
from localization.agents.temporal import _PERMANENT, _localizer_settings
from localization.config import Settings
from localization.providers.llm import model_for
from tests.support.stub_server import Scenario, StubServer

PARAMS = ModelRequestParameters()
MESSAGES = [ModelRequest.user_text_prompt("translate")]


def test_the_cap_grows_with_the_script_and_never_truncates_a_legitimate_answer() -> None:
    caps = [ao.answer_cap(n) for n in (0, 1, 4, 40, 150)]
    assert caps == sorted(caps) and caps[0] == caps[1]
    # Measured with reasoning off: 25 tokens a line. Allow a long segment (about 300).
    for lines in (1, 4, 40, 150):
        assert ao.answer_cap(lines) >= min(ao.CEILING_TOKENS, 150 * lines + 500) > 25 * lines
    # The most a run accepts: a few thousand tokens at most is a legitimate answer.
    assert ao.answer_cap(150) >= 150 * 120
    # Under the runaways that were measured on a 4-line script.
    assert ao.answer_cap(4) < 8_813


class Recording:
    def __init__(self, response: ModelResponse) -> None:
        self.response = response
        self.seen: list[object] = []

    def model(self) -> FunctionModel:
        async def respond(messages, info: AgentInfo) -> ModelResponse:  # noqa: ANN001
            self.seen.append(info.model_settings)
            return self.response

        return FunctionModel(respond, model_name="m")


def answer(finish: str = "stop") -> ModelResponse:
    return ModelResponse(parts=[TextPart("ok")], finish_reason=finish)  # type: ignore[arg-type]


async def test_with_reasoning_off_the_declared_settings_are_sent_as_declared() -> None:
    fake = Recording(answer())
    model = ao.AnswerOnlyModel(fake.model(), reasoning_off=True)
    declared = ao.answer_only_settings(4)
    await model.request(MESSAGES, declared, PARAMS)
    (sent,) = fake.seen
    assert sent["max_tokens"] == ao.answer_cap(4)  # type: ignore[index]
    assert sent["extra_body"] == {"reasoning": {"enabled": False}}  # type: ignore[index]


async def test_with_reasoning_default_the_request_is_the_plain_one() -> None:
    fake = Recording(answer())
    model = ao.AnswerOnlyModel(fake.model(), reasoning_off=False)
    declared = {**ao.answer_only_settings(4), "temperature": 0.2}
    await model.request(MESSAGES, declared, PARAMS)  # type: ignore[arg-type]
    (sent,) = fake.seen
    assert sent == {"temperature": 0.2}


@pytest.mark.parametrize("reasoning_off", [True, False])
async def test_a_request_that_did_not_declare_it_is_never_touched(reasoning_off: bool) -> None:
    """The tool-calling agents share the model: their settings pass as they are, and a
    cut-off answer of theirs is for pydantic-ai to judge."""
    fake = Recording(answer("length"))
    model = ao.AnswerOnlyModel(fake.model(), reasoning_off=reasoning_off)
    for settings in (None, {}, {"max_tokens": 50}, {"extra_body": {"other": 1}}):
        fake.seen.clear()
        response = await model.request(MESSAGES, settings, PARAMS)  # type: ignore[arg-type]
        assert fake.seen == [settings] and response.finish_reason == "length"


async def test_an_answer_cut_off_at_the_cap_is_a_model_error_temporal_retries() -> None:
    model = ao.AnswerOnlyModel(Recording(answer("length")).model(), reasoning_off=True)
    with pytest.raises(ModelAPIError, match="cut off at 2700 tokens") as info:
        await model.request(MESSAGES, ao.answer_only_settings(4), PARAMS)
    with pytest.raises(ApplicationError) as wrapped, model_errors_as_application_errors():
        raise info.value
    assert wrapped.value.non_retryable is False
    assert wrapped.value.type == "ModelAPIError" and wrapped.value.type not in _PERMANENT


async def test_an_answer_that_spent_the_cap_is_cut_off_whatever_its_finish_reason() -> None:
    """A provider reported a looping answer stopped at the cap as `tool_call`."""
    looped = ModelResponse(
        parts=[TextPart("á á á")],
        finish_reason="tool_call",
        usage=RequestUsage(output_tokens=ao.answer_cap(4)),
    )
    model = ao.AnswerOnlyModel(Recording(looped).model(), reasoning_off=True)
    with pytest.raises(ModelAPIError, match="cut off at 2700 tokens"):
        await model.request(MESSAGES, ao.answer_only_settings(4), PARAMS)


async def test_an_answer_under_the_cap_passes() -> None:
    short = ModelResponse(
        parts=[TextPart("ok")],
        finish_reason="tool_call",
        usage=RequestUsage(output_tokens=ao.answer_cap(4) - 1),
    )
    model = ao.AnswerOnlyModel(Recording(short).model(), reasoning_off=True)
    assert await model.request(MESSAGES, ao.answer_only_settings(4), PARAMS) is short


async def test_a_cut_off_answer_is_not_judged_when_the_cap_was_taken_off() -> None:
    model = ao.AnswerOnlyModel(Recording(answer("length")).model(), reasoning_off=False)
    response = await model.request(MESSAGES, ao.answer_only_settings(4), PARAMS)
    assert response.finish_reason == "length"


# --- the request the endpoint receives ---------------------------------------------------


@pytest.fixture
def endpoint(stub: StubServer) -> Iterator[StubServer]:
    yield stub


def chat_answer(finish: str = "stop", provider: str = "Relace") -> dict:
    lines = {"lines": [{"id": 0, "text_vi": "xin chào"}, {"id": 1, "text_vi": "tạm biệt"}]}
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": "deepseek/probe",
        "provider": provider,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "final_result", "arguments": json.dumps(lines)},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


DEPS = LocalizerDeps(
    aweme_id="7001",
    workflow_id="video-7001-g1",
    channel_id="c1",
    transcript_key="work/7001/r0/transcript.json",
    subtitle_key="work/7001/r0/subtitles.json",
    syllables_per_sec=4.0,
    segment_count=2,
)


def single_agent(stub: StubServer, *, reasoning_off: bool):  # noqa: ANN201
    settings = Settings(
        openrouter_base_url=f"{stub.url}/v1",
        openrouter_api_key="k",  # type: ignore[arg-type]
        localizer_model="some/model",  # type: ignore[arg-type]
        voicestudio_allow_private_http_cidrs="127.0.0.0/8",
    )
    model = ao.AnswerOnlyModel(model_for("localizer", settings), reasoning_off=reasoning_off)
    return localizer_single.build_single_agent(
        model, name="localizer_single", instructions="translate", model_settings=_localizer_settings
    )


async def test_the_body_sent_to_the_provider_says_answer_only_and_carries_the_cap(
    endpoint: StubServer,
) -> None:
    endpoint.scenario = Scenario(json_body=chat_answer())
    result = await single_agent(endpoint, reasoning_off=True).run("hello", deps=DEPS)
    assert [line.id for line in result.output.lines] == [0, 1]
    (recorded,) = endpoint.requests
    body = json.loads(recorded.body)
    assert body["reasoning"] == {"enabled": False}
    # The OpenAI client names it `max_completion_tokens`; OpenRouter honours both (checked
    # live: a 30 token cap ends the answer at 30 tokens with finish_reason `length`).
    assert body["max_completion_tokens"] == ao.answer_cap(2)


async def test_switched_back_the_body_has_neither(endpoint: StubServer) -> None:
    endpoint.scenario = Scenario(json_body=chat_answer())
    await single_agent(endpoint, reasoning_off=False).run("hello", deps=DEPS)
    body = json.loads(endpoint.requests[0].body)
    assert (
        "reasoning" not in body and "max_tokens" not in body and "max_completion_tokens" not in body
    )


async def test_an_answer_the_provider_cut_off_fails_instead_of_being_parsed(
    endpoint: StubServer,
) -> None:
    """The cut-off body here still parses as two lines: nothing but the finish reason says
    it is incomplete."""
    endpoint.scenario = Scenario(json_body=chat_answer("length"))
    with pytest.raises(ModelAPIError, match="cut off"):
        await single_agent(endpoint, reasoning_off=True).run("hello", deps=DEPS)

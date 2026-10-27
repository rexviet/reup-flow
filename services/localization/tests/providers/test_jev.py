"""The Jev provider over the real SDK client and a transport that answers with the bodies
OpenRouter's `/systemone` really sends (recorded: the model resolved to a dated id, the
cost and generation id in the body, tokens as input and output)."""

import asyncio
import json

import httpx2
import pytest
from temporalio.exceptions import ApplicationError
from typesafe_sdk import Noul

from localization.observability.llm_usage import LlmUsage
from localization.providers.jev import Jev, make_client

KEY = "sk-or-secret-key-value"
QUESTIONS = {"q1": Noul(instructions="is it so"), "q2": Noul(instructions="is it also so")}

REAL_BODY = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {"q1": {"type": "noul", "noul": 0.96}, "q2": {"type": "noul", "noul": 0.12}},
    "usage": {"input_tokens": 276, "output_tokens": 20, "cost": 0.000011592},
    "id": "gen-dec-1793112141-L0wu0Eq3a75PvrEcqxgl",
    "provider": "TypeSafe",
}


def jev(handler, *, timeout: float = 5.0) -> Jev:  # noqa: ANN001
    client = make_client(
        KEY,
        base_url="https://openrouter.ai/api",
        model="typesafe/jev-1.13",
        transport=httpx2.MockTransport(handler),
    )
    return Jev(client, model="typesafe/jev-1.13", timeout=timeout, secret=KEY)


def answering(body: dict, status: int = 200, headers: dict | None = None):  # noqa: ANN201
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(status, json=body, headers=headers)

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


async def test_the_answers_come_back_as_probabilities_with_what_the_call_used() -> None:
    handler = answering(REAL_BODY)
    result = await jev(handler).ask({"segments": []}, QUESTIONS)
    assert result.answers == {"q1": 0.96, "q2": 0.12}
    assert (result.model, result.input_tokens, result.output_tokens) == (
        "typesafe/jev-1.13-20260917",
        276,
        20,
    )
    (request,) = handler.seen
    assert str(request.url) == "https://openrouter.ai/api/v1/systemone"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    sent = json.loads(request.content)
    assert sent["model"] == "typesafe/jev-1.13"
    assert set(sent["questions"]) == {"q1", "q2"}


async def test_the_cost_and_generation_id_openrouter_sends_are_handed_to_the_recorder() -> None:
    """The SDK's response model drops both; the recorder needs them for llm_calls."""
    got: list[LlmUsage] = []

    async def sink(usage: LlmUsage) -> None:
        got.append(usage)

    await jev(answering(REAL_BODY)).ask({}, QUESTIONS, sink)
    (usage,) = got
    assert usage.generation_id == "gen-dec-1793112141-L0wu0Eq3a75PvrEcqxgl"
    assert usage.cost_usd == pytest.approx(0.000011592)
    assert (usage.prompt_tokens, usage.completion_tokens) == (276, 20)
    assert usage.model == "typesafe/jev-1.13-20260917"


async def test_a_body_without_a_cost_has_no_cost_not_a_guess() -> None:
    body = {**REAL_BODY, "usage": {"input_tokens": 5, "output_tokens": 2}}
    got: list[LlmUsage] = []

    async def sink(usage: LlmUsage) -> None:
        got.append(usage)

    await jev(answering(body)).ask({}, QUESTIONS, sink)
    assert got[0].cost_usd is None and got[0].generation_id


async def test_two_calls_at_once_each_record_their_own_cost() -> None:
    """One client serves several reviews: the cost read from one body is never
    attributed to another call."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        asked = json.loads(request.content)["state"]["n"]
        body = {**REAL_BODY, "id": f"gen-{asked}", "usage": {**REAL_BODY["usage"], "cost": asked}}
        return httpx2.Response(200, json=body)

    j = jev(handler)
    seen: dict[int, LlmUsage] = {}

    async def one(n: int) -> None:
        async def sink(usage: LlmUsage) -> None:
            seen[n] = usage

        await j.ask({"n": n}, QUESTIONS, sink)

    await asyncio.gather(*(one(n) for n in (1, 2, 3, 4)))
    assert {n: (u.generation_id, u.cost_usd) for n, u in seen.items()} == {
        n: (f"gen-{n}", float(n)) for n in (1, 2, 3, 4)
    }


@pytest.mark.parametrize(
    "bad",
    [1.4, -0.1, None, float("nan")],
    ids=["above one", "below zero", "missing", "nan"],
)
async def test_a_probability_outside_zero_to_one_is_refused_for_good_but_still_recorded(
    bad: float | None,
) -> None:
    """The request was billed whatever it said, so the recorder hears of it first."""
    answers = {"q1": {"type": "noul", "noul": bad}, "q2": {"type": "noul", "noul": 0.1}}
    body = {**REAL_BODY, "answers": answers}
    raw = json.dumps(body).replace("NaN", "null")
    got: list[LlmUsage] = []

    async def sink(usage: LlmUsage) -> None:
        got.append(usage)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=raw, headers={"content-type": "application/json"})

    with pytest.raises(ApplicationError) as info:
        await jev(handler).ask({}, QUESTIONS, sink)
    assert (info.value.type, info.value.non_retryable) == ("PermanentProviderError", True)
    assert len(got) == 1


async def test_an_answer_of_the_wrong_kind_or_a_missing_one_is_refused_for_good() -> None:
    choice = {"type": "choice", "choice": "a", "confidence": 0.9, "probabilities": {"a": 1.0}}
    for answers in (
        {"q1": choice, "q2": {"type": "noul", "noul": 0.1}},
        {"q2": {"type": "noul", "noul": 0.1}},
    ):
        body = {**REAL_BODY, "answers": answers}
        with pytest.raises(ApplicationError) as info:
            await jev(answering(body)).ask({}, QUESTIONS)
        assert info.value.non_retryable is True


@pytest.mark.parametrize(
    ("status", "etype", "retryable"),
    [
        (401, "PermanentProviderError", False),
        (403, "PermanentProviderError", False),
        (400, "PermanentProviderError", False),
        (404, "PermanentProviderError", False),
        (422, "PermanentProviderError", False),
        (429, "TransientProviderError", True),
        (500, "TransientProviderError", True),
        (502, "TransientProviderError", True),
        (529, "TransientProviderError", True),
        (408, "TransientProviderError", True),
    ],
)
async def test_http_errors_are_classified_the_way_the_retry_policy_reads_them(
    status: int, etype: str, retryable: bool
) -> None:
    handler = answering({"error": {"message": "nope", "code": status}}, status)
    with pytest.raises(ApplicationError) as info:
        await jev(handler).ask({}, QUESTIONS)
    assert (info.value.type, info.value.non_retryable) == (etype, not retryable)


async def test_the_sdk_does_not_retry_on_its_own() -> None:
    handler = answering({"error": {"message": "down", "code": 503}}, 503)
    with pytest.raises(ApplicationError):
        await jev(handler).ask({}, QUESTIONS)
    assert len(handler.seen) == 1  # type: ignore[attr-defined]


async def test_a_rate_limit_waits_as_long_as_it_is_told_up_to_a_minute() -> None:
    short = answering({"error": {"message": "slow down", "code": 429}}, 429, {"retry-after": "7"})
    with pytest.raises(ApplicationError) as info:
        await jev(short).ask({}, QUESTIONS)
    assert info.value.non_retryable is False
    assert info.value.next_retry_delay is not None
    assert info.value.next_retry_delay.total_seconds() == 7

    long = answering({"error": {"message": "slow down", "code": 429}}, 429, {"retry-after": "900"})
    with pytest.raises(ApplicationError) as info:
        await jev(long).ask({}, QUESTIONS)
    assert info.value.non_retryable is True


async def test_an_auth_failure_says_nothing_of_the_body_and_no_error_carries_the_key() -> None:
    body = {"error": {"message": f"bad key {KEY}", "code": 401}}
    with pytest.raises(ApplicationError) as info:
        await jev(answering(body, 401)).ask({}, QUESTIONS)
    assert KEY not in str(info.value) and "bad key" not in str(info.value)

    body = {"error": {"message": f"echoed {KEY}", "code": 400}}
    with pytest.raises(ApplicationError) as info:
        await jev(answering(body, 400)).ask({}, QUESTIONS)
    assert KEY not in str(info.value)


async def test_a_connection_failure_is_transient() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ApplicationError) as info:
        await jev(handler).ask({}, QUESTIONS)
    assert (info.value.type, info.value.non_retryable) == ("TransientProviderError", False)


async def test_a_call_that_never_answers_is_cut_off_and_is_transient() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=REAL_BODY)

    with pytest.raises(ApplicationError) as info:
        await jev(handler, timeout=0.1).ask({}, QUESTIONS)
    assert (info.value.type, info.value.non_retryable) == ("TransientProviderError", False)
    assert "no answer within" in str(info.value)


async def test_a_body_that_is_not_a_system_one_answer_is_permanent() -> None:
    with pytest.raises(ApplicationError) as info:
        await jev(answering({"unexpected": True})).ask({}, QUESTIONS)
    assert info.value.non_retryable is True

"""The size guard on the Localizer conversation, at the sizes that matter: a
conversation between the guard's limit and the payload limit must be refused too,
because the request carries more than the messages."""

from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from temporalio.exceptions import ApplicationError

from localization.agents.temporal import MAX_CONVERSATION_BYTES, ConversationSizeGuard
from localization.contracts.limits import MAX_PAYLOAD_BYTES


def request_of(kib: int) -> SimpleNamespace:
    return SimpleNamespace(messages=[ModelRequest(parts=[UserPromptPart("x" * (kib * 1024))])])


async def test_a_conversation_well_under_the_limit_is_sent() -> None:
    context = request_of(150)
    assert await ConversationSizeGuard().before_model_request(None, context) is context  # type: ignore[arg-type]


@pytest.mark.parametrize("kib", [200, 220, 250])
async def test_a_conversation_over_the_guard_limit_but_under_the_payload_limit_is_refused(
    kib: int,
) -> None:
    assert MAX_CONVERSATION_BYTES < kib * 1024 < MAX_PAYLOAD_BYTES
    with pytest.raises(ApplicationError) as err:
        await ConversationSizeGuard().before_model_request(None, request_of(kib))  # type: ignore[arg-type]
    assert err.value.type == "PayloadTooLarge"
    assert err.value.non_retryable


def test_the_guard_leaves_a_margin_for_what_a_request_carries_besides_messages() -> None:
    assert MAX_CONVERSATION_BYTES == MAX_PAYLOAD_BYTES * 3 // 4
    assert MAX_CONVERSATION_BYTES == 192 * 1024

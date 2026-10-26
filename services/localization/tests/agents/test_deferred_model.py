"""The model as workflow code sees it, and the one a worker binds."""

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel

from localization.agents.temporal import DeferredModel, bind_model_factory


@pytest.fixture(autouse=True)
def _unbind():
    yield
    for alias in ("alias", "other"):
        bind_model_factory(None, alias)


def _request() -> list[ModelRequest]:
    return [ModelRequest(parts=[UserPromptPart("hi")])]


async def test_a_request_is_answered_by_the_model_the_worker_bound() -> None:
    bind_model_factory(
        lambda: FunctionModel(
            lambda messages, info: ModelResponse(parts=[TextPart("from the bound model")])
        ),
        "alias",
    )
    response = await DeferredModel("alias").request(_request(), None, ModelRequestParameters())
    assert response.parts[0].content == "from the bound model"


async def test_a_request_on_a_worker_with_no_model_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="no model bound"):
        await DeferredModel("alias").request(_request(), None, ModelRequestParameters())


def test_it_carries_only_a_name() -> None:
    assert DeferredModel("alias").model_name == "alias"


async def test_each_agent_gets_the_model_bound_under_its_own_alias() -> None:
    """Two agents share one worker process: the Dubber's request must not be
    answered by the Localizer's model."""
    for alias in ("alias", "other"):
        bind_model_factory(
            lambda alias=alias: FunctionModel(
                lambda messages, info: ModelResponse(parts=[TextPart(f"answered as {alias}")])
            ),
            alias,
        )
    first = await DeferredModel("alias").request(_request(), None, ModelRequestParameters())
    second = await DeferredModel("other").request(_request(), None, ModelRequestParameters())
    assert first.parts[0].content == "answered as alias"
    assert second.parts[0].content == "answered as other"

"""The model as workflow code sees it, and the one a worker binds."""

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel

from localization.agents.temporal import DeferredModel, bind_model_factory


@pytest.fixture(autouse=True)
def _unbind():
    yield
    bind_model_factory(None)


def _request() -> list[ModelRequest]:
    return [ModelRequest(parts=[UserPromptPart("hi")])]


async def test_a_request_is_answered_by_the_model_the_worker_bound() -> None:
    bind_model_factory(
        lambda: FunctionModel(
            lambda messages, info: ModelResponse(parts=[TextPart("from the bound model")])
        )
    )
    response = await DeferredModel("alias").request(_request(), None, ModelRequestParameters())
    assert response.parts[0].content == "from the bound model"


async def test_a_request_on_a_worker_with_no_model_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="no model bound"):
        await DeferredModel("alias").request(_request(), None, ModelRequestParameters())


def test_it_carries_only_a_name() -> None:
    assert DeferredModel("alias").model_name == "alias"

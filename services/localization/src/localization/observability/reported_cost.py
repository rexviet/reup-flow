"""The worker side of a durable agent's request cost (see `model_cost`)."""

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from localization.observability.llm_usage import collect_usage
from localization.observability.model_cost import with_reported_cost


class ReportedCostModel(WrapperModel):
    """The worker's model for a durable agent: its response carries the cost OpenRouter
    reported for it, read by the response hook while the request was made."""

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        with collect_usage() as reported:
            response = await self.wrapped.request(
                messages, model_settings, model_request_parameters
            )
        return with_reported_cost(response, reported)

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.converter import DataConverter

from localization.temporal.size_guard_codec import SizeGuardCodec


def guarded_data_converter() -> DataConverter:
    return DataConverter(payload_codec=SizeGuardCodec())


async def connect(address: str, namespace: str = "default") -> Client:
    """The one way the service connects. The codec rides on the data converter and
    `PydanticAIPlugin` then installs its payload converter around it."""
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=guarded_data_converter(),
        plugins=[PydanticAIPlugin()],
    )

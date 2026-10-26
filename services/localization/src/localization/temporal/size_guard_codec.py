from collections.abc import Iterable, Sequence

from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec
from temporalio.exceptions import ApplicationError

from localization.contracts.limits import MAX_PAYLOAD_BYTES


class SizeGuardCodec(PayloadCodec):
    """Fails any payload over 256 KiB at encode time.

    Payloads carry object keys, never bytes. A codec is the right seam because
    `PydanticAIPlugin` swaps the client's `PayloadConverter` but keeps the codec;
    a size check hung on a converter would be silently discarded.
    """

    def __init__(self, limit: int = MAX_PAYLOAD_BYTES) -> None:
        self._limit = limit

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        self._check(payloads)
        return list(payloads)

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return list(payloads)

    def _check(self, payloads: Iterable[Payload]) -> None:
        for payload in payloads:
            size = len(payload.data)
            if size > self._limit:
                raise ApplicationError(
                    f"payload of {size} bytes exceeds the {self._limit} byte limit; "
                    "store the data in the object store and pass its key",
                    type="PayloadTooLarge",
                    non_retryable=True,
                )

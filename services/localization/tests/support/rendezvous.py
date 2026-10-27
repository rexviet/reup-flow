"""Two activities that can only both finish if they are in flight at the same time.

Starting two activities one after the other and awaiting them later is parallel
dispatch; it says nothing about whether they ever ran together. Each side of a
rendezvous announces that it has started and then waits for the other side to have
started too, so a workflow that ran them one after the other leaves the first waiting
for a second that has not been scheduled, and the first fails with a clear reason
instead of the test passing on timing."""

import asyncio

from temporalio.exceptions import ApplicationError


class Rendezvous:
    def __init__(self, *sides: str, timeout: float = 5.0) -> None:
        self._started = {side: asyncio.Event() for side in sides}
        self._timeout = timeout
        self.met: set[str] = set()

    async def meet(self, side: str) -> None:
        self._started[side].set()
        others = [e for name, e in self._started.items() if name != side]
        try:
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in others)), self._timeout)
        except TimeoutError:
            raise ApplicationError(
                f"{side} was never in flight at the same time as the others",
                type="NoOverlap",
                non_retryable=True,
            ) from None
        self.met.add(side)

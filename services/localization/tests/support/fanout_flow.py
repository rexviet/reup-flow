import asyncio
from datetime import timedelta

from temporalio import workflow


@workflow.defn
class FanOutWorkflow:
    """Starts `count` activities on one queue at once and waits for all of them."""

    @workflow.run
    async def run(self, queue: str, count: int) -> None:
        handles = [
            workflow.start_activity(
                "overlap_probe",
                i,
                task_queue=queue,
                start_to_close_timeout=timedelta(seconds=30),
            )
            for i in range(count)
        ]
        await asyncio.gather(*handles)

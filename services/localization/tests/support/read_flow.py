from datetime import timedelta

from temporalio import workflow


@workflow.defn
class ReadWorkflow:
    @workflow.run
    async def run(self, key: str) -> None:
        # Default retry policy: unlimited attempts. Only a non-retryable error stops it.
        await workflow.execute_activity(
            "read_artifact_counted", key, start_to_close_timeout=timedelta(seconds=10)
        )

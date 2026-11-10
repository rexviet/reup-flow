"""Durable single-cue regeneration; Temporal carries immutable references only."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn
class RevoiceCaptionWorkflow:
    @workflow.run
    async def run(self, request_key: str) -> dict:
        retry = RetryPolicy(maximum_attempts=3)
        try:
            clip = await workflow.execute_activity(
                "revoice_synthesize",
                request_key,
                task_queue="tts",
                result_type=str,
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=retry,
            )
            candidate = await workflow.execute_activity(
                "revoice_mix",
                args=[request_key, clip],
                task_queue="media",
                result_type=str,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=retry,
            )
            return await workflow.execute_activity(
                "revoice_commit",
                args=[request_key, candidate],
                task_queue="io",
                result_type=dict,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry,
            )
        except ActivityError:
            return await workflow.execute_activity(
                "revoice_failed",
                request_key,
                task_queue="io",
                result_type=dict,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=retry,
            )

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


@workflow.defn
class RevoiceBatchWorkflow:
    """Several lines of one project revoiced as one revision: each line's voice is made
    on its own, all at once, and they are mixed in a single pass."""

    @workflow.run
    async def run(self, request_key: str, cue_ids: list[int]) -> dict:
        retry = RetryPolicy(maximum_attempts=3)
        try:
            handles = [
                workflow.start_activity(
                    "revoice_synthesize_cue",
                    args=[request_key, cue_id],
                    task_queue="tts",
                    result_type=str,
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(seconds=60),
                    retry_policy=retry,
                )
                for cue_id in cue_ids
            ]
            # Every line's voice at once; the first to fail for good fails the batch.
            await workflow.wait(handles, return_when="FIRST_EXCEPTION")
            clips = [await handle for handle in handles]
            mixed = await workflow.execute_activity(
                "revoice_mix_batch",
                args=[request_key, list(clips)],
                task_queue="media",
                result_type=dict,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=retry,
            )
            return await workflow.execute_activity(
                "revoice_commit_batch",
                args=[request_key, mixed],
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

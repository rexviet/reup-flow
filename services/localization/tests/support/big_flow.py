"""Workflows that hand a too-large value to an activity or return one, with and
without the pre-check, to pin what Temporal actually does."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from localization.contracts.limits import ensure_payload_fits

TASK_QUEUE = "big-flow"
BIG = 300 * 1024


@workflow.defn
class BigFlow:
    @workflow.run
    async def run(self, mode: str) -> str:
        data = "x" * BIG
        if mode in ("guarded_arg", "guarded_result"):
            ensure_payload_fits(data, "the value")
        if mode in ("unguarded_arg", "guarded_arg"):
            await workflow.execute_activity(
                "noop", data, start_to_close_timeout=timedelta(seconds=10)
            )
            return "done"
        return data  # *_result: the workflow's own result is the oversize payload

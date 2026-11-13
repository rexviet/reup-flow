"""The workflows and activities of test_resume_failed, in a module of their own: Temporal's
workflow sandbox loads the module a workflow is defined in, so it must import nothing heavy."""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

QUEUE = "resume-test"
NO_RETRY = RetryPolicy(maximum_attempts=1)

calls: dict[str, int] = {}
broken = {"transcribe": True}


@activity.defn(name="ingest")
async def ingest() -> str:
    calls["ingest"] = calls.get("ingest", 0) + 1
    return "ingested"


@activity.defn(name="transcribe")
async def transcribe(source: str) -> str:
    calls["transcribe"] = calls.get("transcribe", 0) + 1
    if broken["transcribe"]:
        raise ApplicationError("stt: unexpected response shape", non_retryable=True)
    return f"transcript of {source}"


@workflow.defn(name="ActivityRun")
class ActivityRun:
    @workflow.run
    async def run(self) -> str:
        opts = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": NO_RETRY}
        source = await workflow.execute_activity("ingest", result_type=str, **opts)
        return await workflow.execute_activity("transcribe", source, result_type=str, **opts)


@workflow.defn(name="CodeRun")
class CodeRun:
    """Fails in its own code, with no activity to go on from."""

    @workflow.run
    async def run(self) -> str:
        raise ApplicationError("gave up", non_retryable=True)

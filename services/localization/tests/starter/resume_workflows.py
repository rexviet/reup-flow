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
# What the stand-in skip check answers: True ends the run as skipped, unless set aside.
skip_check = {"skip": True, "bypassed": False}


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


@activity.defn(name="classify_video")
async def classify_video(source: str) -> bool:
    calls["classify_video"] = calls.get("classify_video", 0) + 1
    return skip_check["skip"] and not skip_check["bypassed"]


@workflow.defn(name="GatedRun")
class GatedRun:
    """Prepares, then asks a skip check, the way the localization asks about advertisements."""

    @workflow.run
    async def run(self) -> str:
        opts = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": NO_RETRY}
        source = await workflow.execute_activity("ingest", result_type=str, **opts)
        if await workflow.execute_activity("classify_video", source, result_type=bool, **opts):
            return "skipped"
        return await workflow.execute_activity("transcribe", source, result_type=str, **opts)


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

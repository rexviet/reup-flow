import asyncio
from contextlib import AsyncExitStack

import pytest
from temporalio.worker import Replayer, Worker

from localization.workflows.revoice_caption import RevoiceCaptionWorkflow
from tests.support.revoice import audio_project, reserve


@pytest.mark.parametrize("fail", [False, True])
async def test_revoice_runs_real_activities_on_owned_queues_and_replays(
    temporal_client, review_store, tmp_path, fail
):
    service, acts, provider = audio_project(review_store, tmp_path, fail=fail)
    key = reserve(service)
    async with AsyncExitStack() as stack:
        for queue, functions in [
            ("workflows", []),
            ("tts", [acts.synthesize]),
            ("media", [acts.mix]),
            ("io", [acts.commit, acts.failed]),
        ]:
            await stack.enter_async_context(
                Worker(
                    temporal_client,
                    task_queue=queue,
                    activities=functions,
                    workflows=[RevoiceCaptionWorkflow] if queue == "workflows" else [],
                )
            )
        handle = await temporal_client.start_workflow(
            RevoiceCaptionWorkflow.run,
            key,
            id="revoice-real-activities",
            task_queue="workflows",
        )
        result = await asyncio.wait_for(handle.result(), 45)
        assert result["status"] == ("failed" if fail else "completed")
    history = await handle.fetch_history()
    scheduled = [
        event.activity_task_scheduled_event_attributes
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert {s.activity_type.name: s.task_queue.name for s in scheduled} == (
        {"revoice_synthesize": "tts", "revoice_failed": "io"}
        if fail
        else {"revoice_synthesize": "tts", "revoice_mix": "media", "revoice_commit": "io"}
    )
    assert len(provider.calls) == 1
    replay = Replayer(
        workflows=[RevoiceCaptionWorkflow], data_converter=temporal_client.data_converter
    )
    await replay.replay_workflow(history)

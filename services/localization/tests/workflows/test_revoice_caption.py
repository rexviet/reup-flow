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


class Dispatch:
    """Stands in for the Temporal client: records the batch it is asked to start."""

    def __init__(self):
        self.args = None

    async def start_workflow(self, _run, *, args, **_):
        self.args = args


async def test_a_batch_revoices_the_edited_lines_in_one_revision(
    temporal_client, review_store, tmp_path
):
    """Two lines edited, saved and revoiced at once: each gets its voice made, the mix
    is made once, and a voice too long for its line leaves that line's old one."""
    from localization.review_bridge.models import CueEdit, SaveAndRevoice
    from localization.workflows.revoice_caption import RevoiceBatchWorkflow

    service, acts, provider = audio_project(review_store, tmp_path)
    repo = service.repo
    state = service.project("7001", 1)
    dispatch = Dispatch()
    service.temporal = dispatch
    accepted = await service.save_and_revoice(
        "7001",
        1,
        SaveAndRevoice(
            requestId="batch-request-123456",
            expectedRevision=state["revision"],
            headETag=state["headETag"],
            edits=(
                CueEdit(cueId=17, text="Câu một", start_ms=0, end_ms=1000),
                # 600 ms is too short for the 1.1 s voice: this line keeps its old one.
                CueEdit(cueId=99, text="Câu hai", start_ms=1400, end_ms=2000),
            ),
        ),
    )
    assert accepted["status"] == "accepted" and accepted["cueIds"] == [17, 99]
    saved = repo.current("7001", 1)[1]
    assert [c.text_vi for c in saved.cues] == ["Câu một", "Câu hai"]
    async with AsyncExitStack() as stack:
        for queue, functions in [
            ("workflows", []),
            ("tts", [acts.synthesize_cue]),
            ("media", [acts.mix_batch]),
            ("io", [acts.commit_batch, acts.failed]),
        ]:
            await stack.enter_async_context(
                Worker(
                    temporal_client,
                    task_queue=queue,
                    activities=functions,
                    workflows=[RevoiceBatchWorkflow] if queue == "workflows" else [],
                )
            )
        handle = await temporal_client.start_workflow(
            RevoiceBatchWorkflow.run,
            args=dispatch.args,
            id="revoice-batch-real-activities",
            task_queue="workflows",
        )
        result = await asyncio.wait_for(handle.result(), 45)
    assert result["status"] == "completed" and result["overflowCues"] == [99]
    assert sorted(provider.calls) == [("Câu hai", "voicestudio"), ("Câu một", "voicestudio")]
    current = repo.current("7001", 1)[1]
    assert current.parent_revision_id == saved.revision_id  # one revision for the batch
    assert current.cues[0].spoken_text == "Câu một" and current.cues[0].clip != saved.cues[0].clip
    assert current.cues[1] == saved.cues[1]  # too long: the old voice stays
    assert current.mixed_audio != saved.mixed_audio
    history = await handle.fetch_history()
    mixes = [
        e
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name == "revoice_mix_batch"
    ]
    assert len(mixes) == 1
    replay = Replayer(
        workflows=[RevoiceBatchWorkflow], data_converter=temporal_client.data_converter
    )
    await replay.replay_workflow(history)

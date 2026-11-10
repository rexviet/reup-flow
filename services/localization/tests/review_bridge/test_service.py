import json

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from localization.review_bridge.models import RevoiceCue, SaveCue
from localization.review_bridge.repository import Conflict, Repository
from localization.review_bridge.service import Service


def command(service, text="Sửa câu này", start_ms=0, end_ms=1000):
    state = service.project("7001", 1)
    return SaveCue(
        expectedRevision=state["revision"],
        headETag=state["headETag"],
        text=text,
        start_ms=start_ms,
        end_ms=end_ms,
    )


def test_save_caption_keeps_audio_and_styles_cold_open(companion, tmp_path):
    repo = companion.repo
    original = repo.current("7001", 1)[1]
    folder = repo.folder("7001", 1)
    source = (
        (folder / "index.html")
        .read_text()
        .replace('id="cue-17"', 'id="cue-17" style="color:#123456;font-size:64px"')
    )
    (folder / "index.html").write_text(source)
    (folder / "custom.css").write_text(".caption { opacity: .9 }")
    payload = '</script><img src=x onerror="fetch(1)">\nMột câu dài hơn nhiều'
    old_command = command(companion, payload, start_ms=200, end_ms=700)
    result = companion.save("7001", 1, 17, old_command)
    cue = result["cues"][0]
    assert cue["mismatch"] and cue["spoken_text"] == "Xin chào"
    latest = repo.current("7001", 1)[1]
    assert latest.cues[0].clip == original.cues[0].clip
    assert latest.cues[0].audio_start_ms == original.cues[0].audio_start_ms
    assert latest.mixed_audio == original.mixed_audio
    srt = repo.read_asset(latest.srt).decode()
    assert payload in srt and "00:00:00,200 --> 00:00:00,700" in srt
    with pytest.raises(Conflict):
        companion.save("7001", 1, 17, old_command)
    fresh = Repository(repo.store, tmp_path / "cold")
    fresh.materialize("7001", 1)
    source = (fresh.folder("7001", 1) / "index.html").read_text()
    assert 'style="color:#123456;font-size:64px"' in source
    assert "</script><img" not in source and "&lt;img" in source
    assert (fresh.folder("7001", 1) / "custom.css").read_text() == ".caption { opacity: .9 }"


def test_blank_caption_retains_review_row_and_old_clip(companion):
    result = companion.save("7001", 1, 17, command(companion, text=""))
    cue = result["cues"][0]
    assert cue["id"] == 17 and cue["clip"] is not None and cue["mismatch"]
    assert companion.repo.read_asset(companion.repo.current("7001", 1)[1].srt) == b""


def test_studio_edit_during_save_is_conflict_not_overwrite(companion, monkeypatch):
    repo = companion.repo
    original = repo.prepare
    folder = repo.folder("7001", 1)
    before = repo.current("7001", 1)[0].revision_id

    def race(*args, **kwargs):
        candidate = original(*args, **kwargs)
        (folder / "index.html").write_text("Concurrent Studio edit")
        return candidate

    monkeypatch.setattr(repo, "prepare", race)
    with pytest.raises(Conflict, match="Studio changed"):
        companion.save("7001", 1, 17, command(companion))
    assert (folder / "index.html").read_text() == "Concurrent Studio edit"
    assert repo.current("7001", 1)[0].revision_id == before


@pytest.mark.parametrize("start,end", [(100, 2100), (1000, 1000), (1001, 1000), (-1, 100)])
def test_invalid_timing_rejected(companion, start, end):
    with pytest.raises(ValueError):
        companion.save("7001", 1, 17, command(companion, start_ms=start, end_ms=end))


class Dispatch:
    def __init__(self):
        self.started = {}
        self.duplicate = 0

    async def list_workflows(self, query):
        for _ in []:
            yield None

    async def start_workflow(self, function, key, **kwargs):
        if kwargs["id"] in self.started:
            self.duplicate += 1
            raise WorkflowAlreadyStartedError(kwargs["id"], "RevoiceCaptionWorkflow")
        self.started[kwargs["id"]] = key


async def test_lost_http_response_attaches_same_workflow_and_hash(companion):
    dispatch = Dispatch()
    service = Service(companion.repo, dispatch)
    request = RevoiceCue(
        requestId="repeat-request-123",
        cueId=17,
        baseRevision=service.project("7001", 1)["revision"],
    )
    await service.revoice("7001", 1, request)
    service = Service(companion.repo, dispatch)  # bridge restart, client retries
    await service.revoice("7001", 1, request)
    assert len(dispatch.started) == 1 and dispatch.duplicate == 1
    record = json.loads(
        service.repo.store.get_bytes(next(iter(dispatch.started.values())), produced_by="test")
    )
    assert len(record["hash"]) == 64 and record["command"] == request.model_dump()
    with pytest.raises(Conflict, match="requestId"):
        await service.revoice("7001", 1, request.model_copy(update={"cueId": 42}))


def test_queue_reservations_are_bounded_across_restarts_and_release_on_result(companion):
    from localization.review_bridge.service import QueueFull

    keys = [companion.repo.request_key("7001", 1, f"bounded-request-{i:03}") for i in range(17)]
    for key in keys[:16]:
        companion.claim_slot(key)
    restarted = Service(companion.repo)
    restarted.claim_slot(keys[0])  # retry attaches without consuming a new slot
    with pytest.raises(QueueFull):
        restarted.claim_slot(keys[16])
    companion.repo.store.put_immutable_bytes(
        keys[0].removesuffix(".json") + ".result.json", b'{"status":"completed"}'
    )
    restarted.claim_slot(keys[16])
    slots = companion.repo.read_json("review/bridge-queue.json")
    assert len(slots) == 16 and keys[0] not in slots and keys[16] in slots


def test_unimported_studio_source_is_not_discarded(companion):
    path = companion.repo.folder("7001", 1) / "index.html"
    source = path.read_text().replace("font:20px/28px", "font:72px/28px")
    assert source != path.read_text()
    path.write_text(source)
    before = companion.project("7001", 1)["revision"]
    with pytest.raises(Conflict, match="explicit import"):
        companion.save("7001", 1, 17, command(companion))
    assert companion.project("7001", 1)["revision"] == before and path.read_text() == source

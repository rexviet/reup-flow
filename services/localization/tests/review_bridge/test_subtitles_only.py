"""A subtitles-only project has no voice: its captions are edited and saved like any
other, but nothing is ever revoiced and no line reads as out of sync with a voice."""

import pytest
from tests.support.review_project import project_inputs

from localization.review_bridge.models import CueEdit, RevoiceCue, SaveAndRevoice
from localization.review_bridge.repository import Repository
from localization.review_bridge.service import Service


class Dispatch:
    def __init__(self):
        self.started = []

    async def start_workflow(self, *args, **kwargs):
        self.started.append((args, kwargs))


@pytest.fixture
def subtitles(review_store, tmp_path):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path, subtitles_only=True)
    builder._build(spec, dub, ingest, subs, None)
    repo = Repository(review_store, tmp_path / "review")
    repo.materialize("7001", 1)
    dispatch = Dispatch()
    return Service(repo, dispatch), dispatch


def test_the_project_says_it_has_no_voice_and_no_line_is_out_of_sync(subtitles):
    service, _ = subtitles
    state = service.project("7001", 1)
    assert state["localizeMode"] == "subtitles_only"
    assert [c["mismatch"] for c in state["cues"]] == [False]


async def test_an_edited_line_is_saved_and_never_revoiced(subtitles):
    service, dispatch = subtitles
    state = service.project("7001", 1)
    result = await service.save_and_revoice(
        "7001",
        1,
        SaveAndRevoice(
            requestId="subtitles-batch-123456",
            expectedRevision=state["revision"],
            headETag=state["headETag"],
            edits=(CueEdit(cueId=17, text="Câu đã sửa", start_ms=0, end_ms=1000),),
        ),
    )
    assert result["status"] == "saved" and result["cueIds"] == []
    assert result["project"]["cues"][0]["text_vi"] == "Câu đã sửa"
    assert result["project"]["cues"][0]["mismatch"] is False
    assert not dispatch.started


async def test_a_revoice_of_a_line_is_refused(subtitles):
    service, dispatch = subtitles
    request = RevoiceCue(
        requestId="subtitles-revoice-123",
        cueId=17,
        baseRevision=service.project("7001", 1)["revision"],
    )
    with pytest.raises(ValueError, match="no voice"):
        await service.revoice("7001", 1, request)
    assert not dispatch.started


def test_a_dubbed_project_is_full(companion):
    assert companion.project("7001", 1)["localizeMode"] == "full"

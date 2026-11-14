"""A closed review generation with real audio for media and durable workflow tests."""

from localization.activities.revoice_caption import RevoiceActivities
from localization.activities.synthesize import Synthesizer
from localization.review_bridge.models import RevoiceCue, SaveCue
from localization.review_bridge.repository import Repository
from localization.review_bridge.service import Service
from tests.support.review_project import project_inputs
from tests.support.tts_fakes import make_wav

# The project's music, apart from the voices (440 Hz for the revoiced line, 220 Hz for
# the others).
MUSIC_HZ = 1000


class CueTts:
    def __init__(self, duration=1.1, fail=False):
        self.duration = duration
        self.fail = fail
        self.calls = []

    async def synthesize(self, text, voice):
        self.calls.append((text, voice.provider))
        if self.fail:
            from localization.providers.errors import PermanentProviderError

            raise PermanentProviderError("test provider failure")
        return make_wav(self.duration, frequency=440)


def audio_project(
    store, tmp_path, *, duration=1.1, blank=False, fail=False, music=0.0, settings=None
):
    """`music`: the amplitude of the project's music (silent by default). `settings`:
    collection settings the manifest carries, as `build_review_project` writes them."""
    builder, spec, dub, ingest, subs = project_inputs(
        store, tmp_path, text="" if blank else "Xin chào"
    )
    builder._build(spec, dub, ingest, subs, "engine")
    repo = Repository(store, tmp_path / "review")
    _, base, etag = repo.current("7001", 1)
    old_clip = repo.asset(base, make_wav(0.5, frequency=220), ".wav")
    original = base.cues[0].model_copy(
        update={
            "end_ms": 1000,
            "clip": None if blank else old_clip,
            "audio_duration_ms": 0 if blank else 500,
        }
    )
    second = original.model_copy(
        update={
            "id": 99,
            "source_segment_ids": (99,),
            "text_vi": "Câu sau",
            "spoken_text": "Câu sau",
            "status": "translated",
            "reason": None,
            "start_ms": 1400,
            "end_ms": 2000,
            "audio_start_ms": 1400,
            "audio_duration_ms": 500,
            "clip": old_clip,
        }
    )
    bgm = make_wav(2, frequency=MUSIC_HZ, amplitude=music)
    base = base.model_copy(update={"bgm": repo.asset(base, bgm, ".wav"), **(settings or {})})
    prepared = repo.prepare(base, (original, second), {})
    repo.activate(prepared, etag)
    repo.materialize("7001", 1)
    service = Service(repo)
    state = service.project("7001", 1)
    service.save(
        "7001",
        1,
        17,
        SaveCue(
            expectedRevision=state["revision"],
            headETag=state["headETag"],
            text="Giọng mới",
            start_ms=0,
            end_ms=1000,
        ),
    )
    provider = CueTts(duration=duration, fail=fail)
    activities = RevoiceActivities(store, Synthesizer(store, provider, model="tts-model"))
    return service, activities, provider


def reserve(service, request_id="revoice-test-123456"):
    command = RevoiceCue(
        requestId=request_id, cueId=17, baseRevision=service.project("7001", 1)["revision"]
    )
    key = service.repo.request_key("7001", 1, request_id)
    service.reserve("7001", 1, command, key)
    return key

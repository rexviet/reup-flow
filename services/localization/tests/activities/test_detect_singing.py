"""Which segments are sung: long enough segments are asked about once each, the answer
marks them in a new transcript document, and with no model nothing is asked at all."""

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from localization.activities import detect_singing as module
from localization.activities.detect_singing import DetectSingingActivity
from localization.activities.localizer_ports import read_transcript_document
from localization.contracts import SingingCheck, SttSegment, Transcript, TranscriptDocument
from localization.providers.errors import TransientProviderError
from localization.storage.object_store import work_key
from tests.support.inputs import make_input

SPEC = make_input()
TRANSCRIPT_KEY = work_key(SPEC.aweme_id, 0, "transcript.json")
VOCALS_KEY = work_key(SPEC.aweme_id, 0, "vocals.wav")
TRANSCRIPT = Transcript(transcript_key=TRANSCRIPT_KEY, segment_count=3)
SOURCE_KEY = TRANSCRIPT_KEY.rsplit("/", 1)[0] + "/source.mp4"
CLIPPED: list[bytes] = []


class Detector:
    model_id = "audio/model"

    def __init__(self, sung: set[int], fail_first: bool = False) -> None:
        self.asked: list[int] = []
        self._sung = sung
        self._fail = fail_first

    async def is_singing(self, wav: bytes) -> bool:
        start = int(wav.decode())
        self.asked.append(start)
        if self._fail:
            self._fail = False
            raise TransientProviderError("singing: the reply is not the requested JSON")
        return start in self._sung


@pytest.fixture
def inputs(review_store, monkeypatch):
    segments = (
        SttSegment(id=0, start_ms=0, end_ms=4000, text="唱"),
        SttSegment(id=1, start_ms=4000, end_ms=5400, text="短"),
        SttSegment(id=2, start_ms=6000, end_ms=9000, text="说"),
    )
    doc = TranscriptDocument(language="zh", duration_ms=9000, segments=segments, words=())
    review_store.put_bytes(TRANSCRIPT_KEY, doc.model_dump_json().encode())
    review_store.put_bytes(VOCALS_KEY, b"vocals")

    def clip(audio, start_ms, end_ms, out):
        assert audio.read_bytes() in (b"vocals", b"source")
        CLIPPED.append(audio.read_bytes())
        return str(start_ms).encode()

    CLIPPED.clear()
    monkeypatch.setattr(module, "audio_clip", clip)
    return review_store


def run(store, detector) -> SingingCheck:
    activity = DetectSingingActivity(store, detector)
    return ActivityEnvironment().run(activity.detect_singing, SPEC, TRANSCRIPT, VOCALS_KEY)


async def test_sung_segments_are_marked_in_a_new_document(inputs) -> None:
    detector = Detector(sung={0})
    check = await run(inputs, detector)
    assert check.checked and check.singing_ids == (0,)
    assert check.transcript.transcript_key == work_key(SPEC.aweme_id, 0, "transcript.singing.json")
    doc = read_transcript_document(inputs, check.transcript.transcript_key)
    assert [s.kind for s in doc.segments] == ["singing", "speech", "speech"]
    # The segment under two seconds is never asked: it stays speech.
    assert sorted(detector.asked) == [0, 6000]


async def test_every_answer_is_asked_once(inputs) -> None:
    detector = Detector(sung={0}, fail_first=True)
    with pytest.raises(ApplicationError) as error:
        await run(inputs, detector)
    assert not error.value.non_retryable
    first = await run(inputs, detector)
    again = await run(inputs, detector)
    assert first == again
    assert len(detector.asked) == 3  # one failure, then each long segment once


async def test_nothing_sung_leaves_the_transcript_as_it_is(inputs) -> None:
    check = await run(inputs, Detector(sung=set()))
    assert check == SingingCheck(transcript=TRANSCRIPT, checked=True)


async def test_without_a_model_nothing_is_asked_and_the_check_says_so(inputs) -> None:
    check = await run(inputs, None)
    assert check == SingingCheck(transcript=TRANSCRIPT, checked=False)


def test_the_clip_asked_about_is_the_segments_own_stretch_of_the_audio(tmp_path) -> None:
    from localization.activities.synthesize import wav_duration_ms
    from tests.support.tts_fakes import make_wav

    vocals = tmp_path / "vocals.wav"
    vocals.write_bytes(make_wav(5.0))
    wav = module.audio_clip(vocals, 1000, 3500, tmp_path / "clip.wav")
    assert wav[:4] == b"RIFF" and wav_duration_ms(wav) == 2500
    assert not (tmp_path / "clip.wav").exists()


async def test_the_original_audio_is_used_when_the_source_video_is_stored(inputs) -> None:
    inputs.put_bytes(SOURCE_KEY, b"source")
    check = await run(inputs, Detector(sung={0}))
    assert check.singing_ids == (0,)
    assert set(CLIPPED) == {b"source"}


async def test_the_vocals_are_used_when_no_source_video_is_stored(inputs) -> None:
    await run(inputs, Detector(sung=set()))
    assert set(CLIPPED) == {b"vocals"}

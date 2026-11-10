import asyncio

import pytest
from temporalio.testing import ActivityEnvironment

from localization.activities.assemble_script import AssembleScriptActivity
from localization.activities.review_project import resolve_project
from localization.activities.script_document import read_script
from localization.contracts import LocalizedLine, LocalizedLines, SttSegment, TranscriptDocument
from localization.storage.object_store import require_work_key, work_key_for
from tests.support.review_project import project_inputs


async def test_interleaved_generations_build_from_their_own_source_and_script(
    review_store, tmp_path
):
    inputs = [
        project_inputs(review_store, tmp_path, generation=g, text=f"Generation {g}", segment_id=0)
        for g in (1, 2)
    ]

    async def assemble(item):
        builder, spec, dub, ingest, subs = item
        transcript = TranscriptDocument(
            language="zh",
            duration_ms=2000,
            words=(),
            segments=(SttSegment(id=0, start_ms=0, end_ms=1000, text=f"source {spec.generation}"),),
        )
        key = work_key_for(spec, 0, "transcript.json")
        review_store.put_bytes(key, transcript.model_dump_json().encode())
        result = await ActivityEnvironment().run(
            AssembleScriptActivity(review_store).assemble_script,
            spec,
            key,
            subs.subtitle_key,
            LocalizedLines(lines=(LocalizedLine(id=0, text_vi=f"Generation {spec.generation}"),)),
            0,
            2,
        )
        read = read_script(review_store, result.script.script_key, spec)
        assert read.segments[0].src_zh == f"source {spec.generation}"
        ref = await builder.build_review_project(spec, dub, ingest, subs, None)
        return spec, ref

    results = await asyncio.gather(*(assemble(i) for i in inputs))
    for spec, ref in results:
        _, manifest, _ = resolve_project(review_store, ref.project_key)
        assert manifest.cues[0].text_vi == f"Generation {spec.generation}"
        assert manifest.video.sha256 == spec.source_snapshot.sha256
    first, second = [r[0] for r in results]
    with pytest.raises(ValueError, match="generation"):
        require_work_key(first, work_key_for(second, 0, "script.v2.json"))
    with pytest.raises(ValueError, match="generation"):
        read_script(review_store, work_key_for(second, 0, "script.v2.json"), first)


async def test_interleaved_prep_reads_captured_sources_and_writes_separate_generations(
    review_store, tmp_path
):
    import shutil
    import tempfile
    from pathlib import Path

    from localization.activities.detect_subtitles import DetectSubtitlesActivity
    from localization.activities.ingest import IngestActivity
    from localization.activities.transcribe import TranscribeActivity
    from tests.support.inputs import make_input
    from tests.workflows.test_prep_stage import FakeOcr, FakeStt, _ffmpeg

    class Separator:
        def separate(self, input_path, duration_ms=0, cancel=None):
            root = Path(tempfile.mkdtemp(dir=tmp_path))
            vocals, bgm = root / "vocals.wav", root / "bgm.wav"
            shutil.copyfile(input_path, vocals)
            shutil.copyfile(input_path, bgm)
            return root, vocals, bgm

    specs = []
    for generation, frequency in ((1, 440), (2, 880)):
        video = tmp_path / f"g{generation}.mp4"
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration=2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(video),
        )
        spec = make_input(generation=generation).model_copy(
            update={"delivery_mode": "review_project"}
        )
        review_store.put_file(spec.video_key, video)
        snapshot = review_store.capture_source(
            spec.video_key, f"work/7001/g{generation}/r0/source.mp4"
        )
        specs.append(spec.model_copy(update={"source_snapshot": snapshot}))
    # Both prep stages must use their snapshot even if downloader replaces the source again.
    review_store.put_bytes(specs[0].video_key, b"no longer a video")

    async def prep(spec):
        env = ActivityEnvironment()
        ingest = await env.run(IngestActivity(review_store, Separator()).ingest, spec)
        doc = TranscriptDocument(
            language="zh",
            duration_ms=2000,
            words=(),
            segments=(
                SttSegment(id=0, start_ms=0, end_ms=1000, text=f"generation {spec.generation}"),
            ),
        )
        transcript, subtitles = await asyncio.gather(
            ActivityEnvironment().run(
                TranscribeActivity(review_store, FakeStt(doc)).transcribe, spec, ingest.vocals_key
            ),
            ActivityEnvironment().run(
                DetectSubtitlesActivity(review_store, FakeOcr({})).detect_subtitles, spec
            ),
        )
        for key in (
            ingest.vocals_key,
            ingest.bgm_key,
            transcript.transcript_key,
            subtitles.subtitle_key,
        ):
            require_work_key(spec, key)
        stored = TranscriptDocument.model_validate_json(
            review_store.get_bytes(transcript.transcript_key, produced_by="transcribe")
        )
        assert stored.segments[0].text == f"generation {spec.generation}"
        return review_store.get_bytes(ingest.vocals_key, produced_by="ingest")

    first, second = await asyncio.gather(*(prep(spec) for spec in specs))
    assert first != second

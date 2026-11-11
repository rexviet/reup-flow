import json

import pytest
from temporalio.testing import ActivityEnvironment

from localization.activities.layout_captions import caption_px, line_gap_px, stroke_px
from localization.activities.publish import PublishActivity
from localization.activities.review_project import materialize_project, resolve_project
from localization.contracts import PublishRequest
from localization.storage.object_store import ArtifactMissingError
from tests.support.review_fakes import MemoryRuns, verdict
from tests.support.review_project import project_inputs


@pytest.mark.parametrize("text", ["Xin chào", "", "</script><img src=x onerror=alert(1)>"])
def test_cold_open_has_all_dependencies_and_one_active_track(review_store, tmp_path, text):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path, text=text)
    ref = builder._build(spec, dub, ingest, subs, "engine")
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    assert manifest.voice == spec.voice and manifest.model == "tts-model"
    assert manifest.cues[0].id == 17
    assert manifest.cues[0].source_segment_ids == (17,)
    if not text:
        assert manifest.cues[0].clip is None
    # Expired work/cache and replaced source must not be needed to reopen the bundle.
    for obj in review_store._client.list_objects_v2(Bucket="review-test").get("Contents", []):
        if not obj["Key"].startswith("review/"):
            review_store._client.delete_object(Bucket="review-test", Key=obj["Key"])
    folder = tmp_path / "cold-open"
    materialize_project(review_store, ref.project_key, folder)
    source = (folder / "index.html").read_text()
    assert source.count("<audio ") == 1 and "muted playsinline" in source
    assert 'id="mask-0"' in source
    assert 'rgba(255,255,255,.25)' in source
    assert 'left:20%;width:60%' in source
    px = caption_px(ingest.height)
    assert f"font:{px}px/{line_gap_px(px)}px" in source
    assert f"-webkit-text-stroke:{stroke_px(px)}px #000" in source
    assert source.count('class="clip source-mask"') >= 1
    if text == "Xin chào":
        assert 'top:290px;height:40px' in source
    assert 'white-space:pre;' in source
    assert "</script><img src=x" not in source
    assert ('id="cue-17"' in source) == bool(text)
    assert bool((folder / "captions.srt").read_text()) == bool(text)
    serialized = manifest.model_dump_json()
    # All asset references are immutable; source identity remains explicit provenance.
    assert "cache/" not in serialized
    assert not any(
        k.startswith("output/")
        for k in review_store._client.list_objects_v2(Bucket="review-test").get("Contents", [])
        for k in [k["Key"]]
    )


def test_long_caption_expands_box_instead_of_shrinking_font(review_store, tmp_path):
    import re

    text = "Đây là một câu phụ đề rất dài cần được đặt trong khung. " * 8
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path, text=text)
    ref = builder._build(spec, dub, ingest, subs, "engine")
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    source = review_store.get_bytes(manifest.html.key, produced_by="review_project").decode()
    widths = [float(w) for w in re.findall(r"left:[\d.]+%;width:([\d.]+)%", source)]
    assert min(widths) == 60 and max(widths) > 60
    px = caption_px(ingest.height)
    assert f"font:{px}px/{line_gap_px(px)}px" in source and "font-size:" not in source
    assert "rgba(255,255,255,.25)" in source
    masks = re.findall(
        r'class="clip source-mask" data-start="([\d.]+)" '
        r'data-duration="([\d.]+)" data-track-index="1" style="([^"]+)"', source
    )
    assert float(masks[0][0]) == 0
    assert float(masks[-1][0]) + float(masks[-1][1]) == ingest.duration_ms / 1000
    for before, after in zip(masks, masks[1:], strict=False):
        assert float(before[0]) + float(before[1]) == pytest.approx(float(after[0]))
    assert "width:60%" in masks[-1][2]


@pytest.mark.parametrize("stage", ["asset", "manifest", "head"])
def test_crash_before_head_never_exposes_candidate_then_retry_recovers(
    review_store, tmp_path, monkeypatch, stage
):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    method = (
        "put_immutable_file"
        if stage == "asset"
        else ("put_immutable_bytes" if stage == "manifest" else "compare_and_swap")
    )
    original = getattr(review_store, method)

    def crash(*args, **kwargs):
        if stage != "head" or args[0].endswith("head.json"):
            raise RuntimeError("injected crash")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(review_store, method, crash)
        with pytest.raises(RuntimeError, match="injected"):
            builder._build(spec, dub, ingest, subs, None)
    with pytest.raises(ArtifactMissingError):
        resolve_project(review_store, "review/7001/g1/head.json")
    ref = builder._build(spec, dub, ingest, subs, None)
    assert resolve_project(review_store, ref.project_key)[0].generation == 1


def test_retry_after_head_commit_preserves_a_manual_revision(review_store, tmp_path):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    head, manifest, etag = resolve_project(review_store, ref.project_key)
    revision = "b" * 32
    updated = manifest.model_copy(update={"revision_id": revision})
    data = updated.model_dump_json().encode()
    key = f"review/7001/g1/r{revision}/manifest.json"
    digest = review_store.put_immutable_bytes(key, data)
    next_head = head.model_copy(
        update={"revision_id": revision, "manifest_key": key, "manifest_sha256": digest}
    )
    review_store.compare_and_swap(ref.project_key, next_head.model_dump_json().encode(), etag)
    assert builder._build(spec, dub, ingest, subs, None) == ref
    assert resolve_project(review_store, ref.project_key)[0].revision_id == revision


async def test_db_failure_after_head_commit_is_retried_without_republishing(review_store, tmp_path):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    before = review_store.read_version(ref.project_key)

    class Runs(MemoryRuns):
        async def upsert(self, **row):
            if not self.writes:
                self.writes += 1
                raise RuntimeError("DB unavailable")
            await super().upsert(**row)

    runs = Runs()
    publish = PublishActivity(review_store, runs)
    request = PublishRequest(
        outcome="ready_for_review", project_key=ref.project_key, verdict=verdict()
    )
    with pytest.raises(RuntimeError, match="DB unavailable"):
        await ActivityEnvironment().run(publish.publish, spec, request)
    result = await ActivityEnvironment().run(publish.publish, spec, request)
    assert result.project_key == ref.project_key and result.output_key is None
    assert result.review_key is None
    assert runs.rows[("7001", 1)]["project"] == ref.project_key
    assert review_store.read_version(ref.project_key) == before
    assert json.loads(before[0])["manifest_key"].startswith("review/7001/g1/")


@pytest.mark.parametrize("blank", [True, False])
async def test_blank_or_overflow_project_cannot_be_marked_clean(review_store, tmp_path, blank):
    from temporalio.exceptions import ApplicationError

    builder, spec, dub, ingest, subs = project_inputs(
        review_store, tmp_path, text="" if blank else "Xin chào"
    )
    if not blank:
        raw = json.loads(review_store.get_bytes(dub.segments_key, produced_by="dub_script"))
        raw["segments"][0]["overflow_unresolved"] = True
        review_store.put_bytes(dub.segments_key, json.dumps(raw).encode())
    ref = builder._build(spec, dub, ingest, subs, None)
    runs = MemoryRuns()
    with pytest.raises(ApplicationError, match="requires human review"):
        await ActivityEnvironment().run(
            PublishActivity(review_store, runs).publish,
            spec,
            PublishRequest(
                outcome="ready_for_review", project_key=ref.project_key, verdict=verdict()
            ),
        )
    assert not runs.rows
    result = await ActivityEnvironment().run(
        PublishActivity(review_store, runs).publish,
        spec,
        PublishRequest(outcome="needs_human", project_key=ref.project_key),
    )
    assert result.project_key == ref.project_key and result.output_key is None


def test_saved_html_allows_only_the_managed_native_runtime(review_store, tmp_path):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    source = review_store.get_bytes(manifest.html.key, produced_by='test').decode()
    assert 'http://127.0.0.1:3002/api/runtime.js' in source
    assert "script-src 'unsafe-inline'" not in source
    assert 'https://cdn.jsdelivr.net' not in source
    assert "font-src 'self' data:" in source
    assert "img-src 'self' data:" in source


def test_cold_open_exposes_only_one_root_composition_and_defers_playback_to_runtime(
    review_store, tmp_path
):
    from localization.activities.review_project import checksum

    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    folder = tmp_path / "projection"
    folder.mkdir()
    old = b'<main data-composition-id="review">old revision</main>'
    stale = folder / (checksum(old) + '.html')
    stale.write_bytes(old)
    materialize_project(review_store, ref.project_key, folder)
    assert sorted(p.name for p in folder.glob('*.html')) == ['index.html']
    assert stale.with_name(stale.name + '.blob').read_bytes() == old
    source = (folder / 'index.html').read_text()
    assert 'requestAnimationFrame' not in source
    assert 'mix.play()' not in source
    assert 'video.currentTime' not in source


def test_archiving_refuses_modified_machine_html_dependency(tmp_path):
    from localization.activities.review_project import archive_html_dependencies, checksum

    old = b'generated bytes'
    path = tmp_path / (checksum(old) + '.html')
    path.write_bytes(b'unsaved human edit')
    with pytest.raises(ValueError, match='modified HTML dependency'):
        archive_html_dependencies(tmp_path)
    assert path.read_bytes() == b'unsaved human edit'

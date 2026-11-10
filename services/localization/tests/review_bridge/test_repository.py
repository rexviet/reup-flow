import shutil

import pytest
from tests.review_bridge.test_service import command

from localization.review_bridge.repository import Conflict, Repository, safe_path
from localization.review_bridge.visual import capture_visual


def test_cas_only_one_writer_and_committed_ancestor_recovers(companion):
    repo = companion.repo
    _, base, etag = repo.current("7001", 1)
    one = repo.prepare(base, base.cues, {})
    two = repo.prepare(base, base.cues, {})
    assert repo.activate(one, etag) == one.revision_id
    with pytest.raises(Conflict):
        repo.activate(two, etag)
    companion.save("7001", 1, 17, command(companion))
    assert repo.activate(one, etag) == one.revision_id
    assert repo.current("7001", 1)[0].revision_id != one.revision_id


def test_crash_after_cas_and_cold_open_recovers_durable_head(companion, monkeypatch, tmp_path):
    repo = companion.repo
    import localization.review_bridge.service as service_module

    def crash(*args):
        raise RuntimeError("crash after CAS before local projection")

    with monkeypatch.context() as patch:
        patch.setattr(service_module, "atomic_write", crash)
        with pytest.raises(RuntimeError, match="after CAS"):
            companion.save("7001", 1, 17, command(companion, text="Bản đã lưu"))
    # Existing clean cache recovers too; no need to delete it.
    repo.materialize("7001", 1)
    assert "Bản đã lưu" in (repo.folder("7001", 1) / "index.html").read_text()
    shutil.rmtree(repo.root)
    fresh = Repository(repo.store, tmp_path / "new-cache")
    fresh.materialize("7001", 1)
    assert fresh.current("7001", 1)[1].cues[0].text_vi == "Bản đã lưu"


def test_dirty_cache_and_symlink_are_preserved(companion, tmp_path):
    repo = companion.repo
    folder = repo.folder("7001", 1)
    (folder / "index.html").write_text("unsaved")
    with pytest.raises(Conflict, match="Save Studio"):
        repo.materialize("7001", 1)
    assert (folder / "index.html").read_text() == "unsaved"
    (folder / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        safe_path(folder, "escape/outside")
    with pytest.raises(ValueError, match="outside"):
        safe_path(folder, "../../outside")


def test_visual_merge_uses_stable_ids_after_word_count_change(companion):
    repo = companion.repo
    _, base, etag = repo.current("7001", 1)
    second = base.cues[0].model_copy(update={"id": 99, "start_ms": 1300, "end_ms": 1900})
    candidate = repo.prepare(base, (*base.cues, second), {})
    repo.activate(candidate, etag)
    repo.materialize("7001", 1)
    path = repo.folder("7001", 1) / "index.html"
    path.write_text(path.read_text().replace('id="cue-99"', 'id="cue-99" style="opacity:0.5"'))
    companion.save("7001", 1, 17, command(companion, text="Một câu sớm dài hơn rất nhiều từ"))
    current = repo.current("7001", 1)[1]
    styles = capture_visual(repo.read_asset(current.html), current.cues)
    assert styles["cue-99"] == {"opacity": "0.5"}
    with pytest.raises(ValueError, match="overlaps"):
        companion.save("7001", 1, 17, command(companion, end_ms=1400))


def test_older_materializer_restores_additive_projection_assets(companion, tmp_path):
    from localization.activities.review_project import materialize_project

    repo = companion.repo
    _, base, etag = repo.current("7001", 1)
    stylesheet = repo.asset(base, b".caption{opacity:.9}", ".css")
    candidate = repo.prepare(base, base.cues, {}, studio_files={"visual/style.css": stylesheet})
    repo.activate(candidate, etag)
    folder = tmp_path / "original-materializer"
    materialize_project(repo.store, "review/7001/g1/head.json", folder)
    assert (folder / "visual/style.css").read_bytes() == b".caption{opacity:.9}"


def test_native_preview_caches_do_not_dirty_projection(companion):
    repo = companion.repo
    folder = repo.folder("7001", 1)
    before = repo.projection(folder)
    for name in (
        ".hyperframes/preview/frame.html",
        ".hyperframes/hf-ids-stamped.json",
        ".waveform-cache/mix.json",
    ):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("transient")
    assert repo.projection(folder) == before
    source = (folder / "index.html").read_text()
    assert 'data-hf-id="hf-cue-17"' in source
    sidecar = folder / ".hyperframes/overrides.json"
    sidecar.write_text("{}")
    assert ".hyperframes/overrides.json" in repo.projection(folder)

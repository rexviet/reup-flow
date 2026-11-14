"""A Studio save rebuilds the project from its manifest alone: the collection's
settings must come through every revision, and a project without them stays as it was."""

from tests.support.review_project import project_inputs

from localization.contracts import CollectionSnapshot
from localization.review_bridge.models import SaveCue
from localization.review_bridge.repository import Repository
from localization.review_bridge.service import Service

SETTINGS = dict(
    caption_color="#00ff88", cover_color="#202020", cover_opacity_pct=80, bgm_offset_db=-6.0
)


def studio(store, tmp_path, collection=None) -> Service:
    builder, spec, dub, ingest, subs = project_inputs(store, tmp_path, collection=collection)
    builder._build(spec, dub, ingest, subs, "engine")
    repo = Repository(store, tmp_path / "review")
    repo.materialize("7001", 1)
    return Service(repo)


def save_text(service: Service, text: str) -> None:
    state = service.project("7001", 1)
    service.save(
        "7001",
        1,
        17,
        SaveCue(
            expectedRevision=state["revision"],
            headETag=state["headETag"],
            text=text,
            start_ms=0,
            end_ms=1000,
        ),
    )


def current(service: Service):
    _, manifest, _ = service.repo.current("7001", 1)
    return manifest, service.repo.read_asset(manifest.html).decode()


def test_a_studio_save_keeps_the_collection_settings(review_store, tmp_path) -> None:
    collection = CollectionSnapshot(
        id="0b6f3c1e-2d4a-4f8e-9a51-7c2e8d9f0a13", name="x", **SETTINGS
    )
    service = studio(review_store, tmp_path, collection)
    first, _ = current(service)
    save_text(service, "Câu đã sửa")
    save_text(service, "Câu sửa lần hai")
    manifest, html = current(service)
    assert manifest.revision_id != first.revision_id
    assert manifest.cues[0].text_vi == "Câu sửa lần hai"
    assert {k: getattr(manifest, k) for k in SETTINGS} == SETTINGS
    assert "color:#00ff88;" in html and "color:#ffff00" not in html
    assert ".source-mask{background:rgba(32,32,32,0.8)}" in html
    assert "backdrop-filter" not in html
    # The local Studio projection is the saved revision.
    folder = tmp_path / "review" / "7001" / "g1"
    assert (folder / "index.html").read_text() == html


def test_a_project_from_before_collections_saves_as_it_always_looked(
    review_store, tmp_path
) -> None:
    service = studio(review_store, tmp_path)
    save_text(service, "Câu đã sửa")
    manifest, html = current(service)
    assert (manifest.caption_color, manifest.cover_color, manifest.cover_opacity_pct) == (
        None,
        None,
        None,
    )
    assert "color:#ffff00;" in html
    assert "background:rgba(255,255,255,.25);backdrop-filter:blur(12px)" in html


GEOMETRY = dict(cover_offset_pct=3, cover_width_pct=100, cover_height_pct=None)
BOX = ".caption,.source-mask{position:absolute;left:0%;width:100%;"


def test_a_studio_save_keeps_the_collection_cover_box(review_store, tmp_path) -> None:
    collection = CollectionSnapshot(id="0b6f3c1e-2d4a-4f8e-9a51-7c2e8d9f0a13", name="x", **GEOMETRY)
    service = studio(review_store, tmp_path, collection)
    save_text(service, "Câu đã sửa")
    manifest, html = current(service)
    assert {k: getattr(manifest, k) for k in GEOMETRY} == GEOMETRY
    assert BOX in html and ".caption{top:301px;height:40px;" in html


def test_a_new_revision_can_be_drawn_with_another_cover_box(review_store, tmp_path) -> None:
    from localization.activities.cover_geometry import CoverGeometry

    service = studio(review_store, tmp_path)
    repo = service.repo
    _, base, _ = repo.current("7001", 1)
    candidate = repo.prepare(
        base, base.cues, {}, operation="cover-relayout", geometry=CoverGeometry(3, 100, None)
    )
    assert {k: getattr(candidate, k) for k in GEOMETRY} == GEOMETRY
    assert candidate.parent_revision_id == base.revision_id
    assert candidate.operation_id == "cover-relayout"
    assert BOX in repo.read_asset(candidate.html).decode()
    # Back to the automatic box: the geometry leaves the manifest again.
    automatic = repo.prepare(candidate, candidate.cues, {}, geometry=CoverGeometry())
    assert (automatic.cover_offset_pct, automatic.cover_width_pct) == (None, None)
    assert repo.read_asset(automatic.html) == repo.read_asset(base.html)

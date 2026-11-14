"""The collection's settings in the review project: the manifest carries them, and the
HTML the final render is made from draws them. A project without them looks as before."""

import pytest

from localization.activities.review_project import resolve_project
from localization.contracts import CollectionSnapshot
from tests.support.review_project import project_inputs

COLLECTION = "0b6f3c1e-2d4a-4f8e-9a51-7c2e8d9f0a13"
# The HTML the builder wrote for these inputs before collections existed (computed
# with the code of that time). A project without settings must come out byte for byte
# the same.
HTML_BEFORE_COLLECTIONS = {
    "Xin chào": "7114dc5acc8e4907484e1cc93c8d91dd3b19d3b50e9ac0418d67bb09ed88ea9a",
    "": "4e8f64a9099bcebaa6178084a3df9def03e195bd0b30e2f4de65040dc5974e5d",
}
WHITE_BLUR = "background:rgba(255,255,255,.25);backdrop-filter:blur(12px)"


def build(store, tmp_path, collection=None, text="Xin chào"):
    builder, spec, dub, ingest, subs = project_inputs(
        store, tmp_path, text=text, collection=collection
    )
    ref = builder._build(spec, dub, ingest, subs, "engine")
    manifest = resolve_project(store, ref.project_key)[1]
    html = store.get_bytes(manifest.html.key, produced_by="test").decode()
    return manifest, html


def snapshot(**settings) -> CollectionSnapshot:
    return CollectionSnapshot(id=COLLECTION, name="成名的代价", **settings)


@pytest.mark.parametrize("text", ["Xin chào", ""])
def test_a_run_without_a_collection_writes_the_project_it_always_did(
    review_store, tmp_path, text
) -> None:
    manifest, html = build(review_store, tmp_path, text=text)
    assert (
        manifest.caption_color,
        manifest.cover_color,
        manifest.cover_opacity_pct,
        manifest.bgm_offset_db,
    ) == (None, None, None, None)
    assert manifest.html.sha256 == HTML_BEFORE_COLLECTIONS[text]


def test_a_collection_that_sets_nothing_keeps_the_yellow_text_and_the_white_blur(
    review_store, tmp_path
) -> None:
    manifest, html = build(review_store, tmp_path, snapshot())
    assert manifest.html.sha256 == HTML_BEFORE_COLLECTIONS["Xin chào"]
    assert "color:#ffff00;" in html and WHITE_BLUR in html


def test_the_project_carries_and_draws_the_collection_settings(review_store, tmp_path) -> None:
    manifest, html = build(
        review_store,
        tmp_path,
        snapshot(
            caption_color="#00ff88", cover_color="#202020", cover_opacity_pct=80, bgm_offset_db=-6
        ),
    )
    assert (
        manifest.caption_color,
        manifest.cover_color,
        manifest.cover_opacity_pct,
        manifest.bgm_offset_db,
    ) == ("#00ff88", "#202020", 80, -6.0)
    assert "color:#00ff88;" in html and "color:#ffff00" not in html
    # A solid box of the cover colour at its opacity, nothing blurred behind it.
    assert ".source-mask{background:rgba(32,32,32,0.8)}" in html
    assert "backdrop-filter" not in html and "rgba(255,255,255" not in html


@pytest.mark.parametrize(
    ("settings", "cover"),
    [
        ({"cover_color": "#202020"}, ("#202020", 92, "rgba(32,32,32,0.92)")),
        ({"cover_opacity_pct": 0}, ("#101010", 0, "rgba(16,16,16,0)")),
        ({"cover_opacity_pct": 100}, ("#101010", 100, "rgba(16,16,16,1)")),
    ],
)
def test_a_partial_cover_setting_is_completed_from_the_worker_cover(
    review_store, tmp_path, settings, cover
) -> None:
    manifest, html = build(review_store, tmp_path, snapshot(**settings))
    assert (manifest.cover_color, manifest.cover_opacity_pct) == cover[:2]
    assert f".source-mask{{background:{cover[2]}}}" in html
    assert "backdrop-filter" not in html

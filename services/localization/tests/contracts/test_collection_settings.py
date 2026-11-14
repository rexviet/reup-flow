"""A run's collection settings, captured into the workflow input at dispatch: their
grammar, and that adding them changes nothing for inputs and specs written before."""

import json

import pytest
from pydantic import ValidationError
from tests.support.inputs import make_input

from localization.contracts import (
    Caption,
    CollectionSnapshot,
    EditSpec,
    LocalizationInput,
    ReviewProjectManifest,
)
from localization.contracts.spec_hash import canonical_spec_json, spec_sha256

COLLECTION = "0b6f3c1e-2d4a-4f8e-9a51-7c2e8d9f0a13"

SNAPSHOT = {
    "id": COLLECTION,
    "name": "成名的代价",
    "bgm_offset_db": -6.5,
    "caption_color": "#00ff88",
    "cover_color": "#202020",
    "cover_opacity_pct": 80,
}

# An EditSpec as the layout wrote it before captions carried a colour, and the hash
# the code of that time gave it. Renders are keyed by this hash, so it must not move.
OLD_SPEC = {
    "source_key": "work/7001/g1/r0/source.mp4",
    "audio_key": "work/7001/g1/r1/dubbed.wav",
    "audio_sha256": "a" * 64,
    "script_version": 2,
    "width": 720,
    "height": 1280,
    "fps": 30,
    "duration_ms": 4000,
    "cover": {
        "region": {"x": 40, "y": 1000, "w": 640, "h": 120},
        "color": "#101010",
        "opacity_pct": 92,
        "only_with_captions": True,
    },
    "caption_box": {"x": 40, "y": 1000, "w": 640, "h": 120},
    "captions": [
        {"segment_id": 0, "start_ms": 0, "end_ms": 1500, "text": "Xin chào", "font_px": 40},
        {"segment_id": 1, "start_ms": 1500, "end_ms": 3000, "text": "các bạn\nnhé", "font_px": 40},
    ],
    "font": "BeVietnamPro-Bold.ttf",
    "font_sha256": "b" * 64,
}
OLD_SPEC_SHA256 = "d74f95436c1fb14546b354414add2a2b2e956120a77e4cfec6c932691a2fa9f9"


def test_a_snapshot_holds_the_collection_settings() -> None:
    snapshot = CollectionSnapshot.model_validate(SNAPSHOT)
    assert snapshot.caption_color == "#00ff88" and snapshot.cover_opacity_pct == 80
    assert snapshot.bgm_offset_db == -6.5


def test_every_setting_of_a_snapshot_may_be_unset() -> None:
    snapshot = CollectionSnapshot(id=COLLECTION, name="Kênh")
    assert (
        snapshot.bgm_offset_db,
        snapshot.caption_color,
        snapshot.cover_color,
        snapshot.cover_opacity_pct,
    ) == (None, None, None, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caption_color", "#FFFF00"),  # the DB stores lower case; anything else is not ours
        ("cover_color", "#Ab12cd"),
        ("caption_color", "ffff00"),
        ("cover_color", "#fff"),
        ("cover_opacity_pct", 101),
        ("cover_opacity_pct", -1),
        ("cover_opacity_pct", 50.5),  # an integer, so no float lands in a spec
        ("bgm_offset_db", 24.5),
        ("bgm_offset_db", -25),
        ("bgm_offset_db", 0.3),  # steps of 0.5, as the DB allows
        ("bgm_offset_db", float("nan")),
        ("id", "not-a-uuid"),
        ("id", COLLECTION.upper()),
        ("name", ""),
        ("name", "x" * 201),
    ],
)
def test_a_snapshot_refuses_what_the_collection_table_cannot_hold(field: str, value) -> None:
    with pytest.raises(ValidationError):
        CollectionSnapshot.model_validate({**SNAPSHOT, field: value})


def test_a_snapshot_refuses_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CollectionSnapshot.model_validate({**SNAPSHOT, "save_subdir": "Drama"})


def test_an_input_written_before_collections_still_decodes_without_one() -> None:
    old = make_input().model_dump(mode="json")
    old.pop("collection", None)
    spec = LocalizationInput.model_validate_json(json.dumps(old))
    assert spec.collection is None


def test_an_input_carries_its_collection_snapshot() -> None:
    spec = make_input().model_copy(update={"collection": CollectionSnapshot(**SNAPSHOT)})
    again = LocalizationInput.model_validate_json(spec.model_dump_json())
    assert again.collection == CollectionSnapshot(**SNAPSHOT)


def test_an_old_spec_keeps_its_hash_and_renders_white() -> None:
    spec = EditSpec.model_validate(OLD_SPEC)
    assert all(c.caption_color is None for c in spec.captions)
    assert spec_sha256(spec) == OLD_SPEC_SHA256
    assert "caption_color" not in canonical_spec_json(spec)


def test_a_coloured_caption_is_a_different_render() -> None:
    spec = EditSpec.model_validate(OLD_SPEC)
    coloured = spec.model_copy(
        update={
            "captions": tuple(
                c.model_copy(update={"caption_color": "#ffff00"}) for c in spec.captions
            )
        }
    )
    assert spec_sha256(coloured) != OLD_SPEC_SHA256
    assert '"caption_color":"#ffff00"' in canonical_spec_json(coloured)


@pytest.mark.parametrize("colour", ["#FFFF00", "yellow", "#ffff0"])
def test_a_caption_refuses_a_colour_that_is_not_lower_case_hex(colour: str) -> None:
    with pytest.raises(ValidationError):
        Caption(segment_id=0, start_ms=0, end_ms=1, text="x", font_px=20, caption_color=colour)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("caption_color", "#FFFF00"),
        ("cover_color", "#12345G"),
        ("cover_opacity_pct", 0.5),
        ("cover_opacity_pct", 101),
        ("bgm_offset_db", 30.0),
        ("bgm_offset_db", 1.25),
    ],
)
def test_the_manifest_refuses_settings_the_collection_could_not_have(
    review_store, tmp_path, field, value
) -> None:
    data = _manifest(review_store, tmp_path).model_dump()
    # Control: the same manifest with a valid value of that setting is accepted.
    valid = {
        "caption_color": "#ffff00",
        "cover_color": "#123456",
        "cover_opacity_pct": 75,
        "bgm_offset_db": -6.5,
    }
    # The cover's colour and opacity are set together.
    data = {**data, "cover_color": "#123456", "cover_opacity_pct": 75}
    accepted = ReviewProjectManifest.model_validate({**data, field: valid[field]})
    assert getattr(accepted, field) == valid[field]
    with pytest.raises(ValidationError):
        ReviewProjectManifest.model_validate({**data, field: value})


@pytest.mark.parametrize("missing", ["cover_color", "cover_opacity_pct"])
def test_the_manifest_sets_cover_colour_and_opacity_together(review_store, tmp_path, missing):
    data = _manifest(review_store, tmp_path).model_dump()
    both = {**data, "cover_color": "#123456", "cover_opacity_pct": 75}
    assert ReviewProjectManifest.model_validate(both).cover_color == "#123456"
    with pytest.raises(ValidationError, match="together"):
        ReviewProjectManifest.model_validate({**both, missing: None})


def test_a_manifest_written_before_collections_decodes_with_no_settings(
    review_store, tmp_path
) -> None:
    data = _manifest(review_store, tmp_path).model_dump(mode="json")
    for name in ("caption_color", "cover_color", "cover_opacity_pct", "bgm_offset_db"):
        data.pop(name, None)
    old = ReviewProjectManifest.model_validate_json(json.dumps(data))
    assert (old.caption_color, old.cover_color, old.cover_opacity_pct, old.bgm_offset_db) == (
        None,
        None,
        None,
        None,
    )


def _manifest(store, tmp_path) -> ReviewProjectManifest:
    from tests.support.review_project import project_inputs

    from localization.activities.review_project import resolve_project

    builder, spec, dub, ingest, subs = project_inputs(store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    return resolve_project(store, ref.project_key)[1]

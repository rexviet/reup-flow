import pytest
from pydantic import ValidationError
from tests.support.review_project import project_inputs

from localization.activities.review_project import resolve_project
from localization.contracts import ReviewHead, ReviewProjectManifest
from localization.contracts.export import main


def test_both_new_and_legacy_schemas_are_current():
    assert main(["--check"]) == 0


@pytest.mark.parametrize("field", ["video", "mixed_audio", "font", "srt", "script", "dub"])
def test_manifest_rejects_external_or_other_generation_dependencies(review_store, tmp_path, field):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    data = manifest.model_dump()
    data[field]["key"] = data[field]["key"].replace("/g1/", "/g2/")
    with pytest.raises(ValidationError, match="this generation"):
        ReviewProjectManifest.model_validate(data)


def test_head_rejects_manifest_outside_its_revision():
    with pytest.raises(ValidationError, match="identity"):
        ReviewHead.model_validate(
            dict(
                aweme_id="7001",
                generation=1,
                revision_id="a" * 32,
                manifest_key="review/7001/g2/manifest.json",
                manifest_sha256="b" * 64,
                source=dict(
                    source_key="source/7001.mp4",
                    etag="e",
                    snapshot_key="work/7001/g1/r0/source.mp4",
                    sha256="b" * 64,
                ),
            )
        )


@pytest.mark.parametrize(
    "name",
    [
        "../outside.css",
        "/absolute.css",
        "./head.json",
        "styles/../head.json",
        "styles//theme.css",
        "styles/./theme.css",
        "head.json",
        "theme.txt",
        ".css",
    ],
)
def test_manifest_rejects_noncanonical_or_reserved_studio_paths(review_store, tmp_path, name):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    data = manifest.model_dump()
    data["studio_files"] = {name: data["html"]}
    with pytest.raises(ValidationError, match="invalid Studio projection file"):
        ReviewProjectManifest.model_validate(data)


def test_manifest_accepts_canonical_nested_studio_paths(review_store, tmp_path):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    ref = builder._build(spec, dub, ingest, subs, None)
    _, manifest, _ = resolve_project(review_store, ref.project_key)
    data = manifest.model_dump()
    data["studio_files"] = {"visual/theme.css": data["html"]}
    assert "visual/theme.css" in ReviewProjectManifest.model_validate(data).studio_files

"""Smoke verification must inspect durable bytes, including corrupt dependencies."""

import pytest

from localization.starter import verify_delivery
from tests.support.review_project import project_inputs


@pytest.mark.parametrize("corrupt", [False, True])
async def test_verifier_materializes_every_dependency(review_store, tmp_path, monkeypatch, corrupt):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    await builder.build_review_project(spec, dub, ingest, subs, None)
    from localization.activities.review_project import resolve_project

    _, manifest, _ = resolve_project(review_store, "review/7001/g1/head.json")
    if corrupt:
        review_store.put_bytes(manifest.mixed_audio.key, b"corrupt")
    monkeypatch.setattr(verify_delivery.ObjectStore, "from_settings", lambda _: review_store)
    monkeypatch.setattr("sys.argv", ["verify_delivery", "7001", "1"])
    if corrupt:
        with pytest.raises(ValueError, match="checksum"):
            verify_delivery.main()
    else:
        verify_delivery.main()

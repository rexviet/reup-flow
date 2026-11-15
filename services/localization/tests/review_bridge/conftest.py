import pytest
from tests.support.review_project import project_inputs

from localization.review_bridge.repository import Repository
from localization.review_bridge.service import Service


@pytest.fixture
def companion(review_store, tmp_path):
    builder, spec, dub, ingest, subs = project_inputs(review_store, tmp_path)
    builder._build(spec, dub, ingest, subs, "engine")
    repo = Repository(review_store, tmp_path / "review")
    repo.materialize("7001", 1)
    return Service(repo)

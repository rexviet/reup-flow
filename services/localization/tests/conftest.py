from collections.abc import AsyncIterator, Iterator

import boto3
import pytest
import pytest_asyncio
from moto import mock_aws
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from localization.config import Settings
from localization.storage.object_store import ObjectStore
from localization.temporal.client import connect
from tests.support.stub_server import StubServer


@pytest.fixture(autouse=True)
def clean_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's real .env must not leak into a test's `Settings()`."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def stub() -> Iterator[StubServer]:
    server = StubServer().start()
    yield server
    server.stop()


@pytest_asyncio.fixture
async def temporal_client() -> AsyncIterator[Client]:
    """A local Temporal server, reached through the service's own `connect`, so the
    codec and the plugin are exactly the production ones."""
    env = await WorkflowEnvironment.start_local()
    try:
        yield await connect(env.client.service_client.config.target_host)
    finally:
        await env.shutdown()


@pytest.fixture
def review_store():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="review-test")
        yield ObjectStore(client, "review-test")

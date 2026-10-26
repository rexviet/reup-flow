import uuid
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.worker import Worker

from localization.config import Settings
from localization.storage.object_store import ArtifactMissingError, ObjectStore, work_key
from tests.support.read_flow import ReadWorkflow

BUCKET = "reup-media"


@pytest.fixture
def store() -> ObjectStore:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield ObjectStore(client, BUCKET)


def test_bytes_round_trip(store: ObjectStore) -> None:
    key = work_key("123", 1, "script.json")
    assert store.exists(key) is False
    store.put_bytes(key, b'{"a":1}', content_type="application/json")
    assert store.exists(key) is True
    assert store.get_bytes(key, produced_by="localize") == b'{"a":1}'


def test_put_is_idempotent_for_the_same_key(store: ObjectStore) -> None:
    key = work_key("123", 1, "x.bin")
    store.put_bytes(key, b"one")
    store.put_bytes(key, b"two")
    assert store.get_bytes(key, produced_by="s") == b"two"


def test_file_round_trip(store: ObjectStore, tmp_path: Path) -> None:
    src = tmp_path / "in.wav"
    src.write_bytes(b"RIFF" * 1000)
    store.put_file("cache/tts/" + "a" * 64 + ".wav", src)
    dest = tmp_path / "out.wav"
    store.download_file("cache/tts/" + "a" * 64 + ".wav", dest, produced_by="synthesize")
    assert dest.read_bytes() == src.read_bytes()


def test_copy_makes_the_same_bytes_available_under_the_new_key(store: ObjectStore) -> None:
    store.put_bytes("source/1.mp4", b"video")
    store.copy("source/1.mp4", work_key("1", 0, "source.mp4"), produced_by="ingest")
    assert store.get_bytes(work_key("1", 0, "source.mp4"), produced_by="x") == b"video"
    assert store.get_bytes("source/1.mp4", produced_by="x") == b"video"


def test_copying_a_missing_object_names_the_producing_stage(store: ObjectStore) -> None:
    with pytest.raises(ArtifactMissingError) as info:
        store.copy("source/gone.mp4", work_key("1", 0, "source.mp4"), produced_by="ingest")
    assert info.value.stage == "ingest"
    assert not store.exists(work_key("1", 0, "source.mp4"))


def test_missing_artifact_names_the_producing_stage(store: ObjectStore, tmp_path: Path) -> None:
    with pytest.raises(ArtifactMissingError) as info:
        store.get_bytes("work/1/r0/gone.json", produced_by="detect_subtitles")
    assert info.value.stage == "detect_subtitles"
    assert info.value.key == "work/1/r0/gone.json"
    assert info.value.non_retryable is True
    assert info.value.type == "ArtifactMissingError"
    with pytest.raises(ArtifactMissingError) as dl:
        store.download_file("work/1/r0/gone.wav", tmp_path / "x", produced_by="synthesize")
    assert dl.value.stage == "synthesize"


def test_other_storage_errors_are_not_reported_as_missing() -> None:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        # No bucket: NoSuchBucket is a configuration problem, not a missing artifact.
        with pytest.raises(Exception) as info:
            ObjectStore(client, "no-such-bucket").get_bytes("k", produced_by="s")
    assert not isinstance(info.value, ArtifactMissingError)


_ATTEMPTS: list[int] = []


@activity.defn(name="read_artifact_counted")
async def _read_artifact_counted(key: str) -> bytes:
    _ATTEMPTS.append(1)
    raise ArtifactMissingError(key, "localize")


async def test_missing_artifact_is_not_retried_and_surfaces_the_stage(
    temporal_client: Client,
) -> None:
    _ATTEMPTS.clear()
    async with Worker(
        temporal_client,
        task_queue="store",
        workflows=[ReadWorkflow],
        activities=[_read_artifact_counted],
    ):
        with pytest.raises(WorkflowFailureError) as info:
            await temporal_client.execute_workflow(
                ReadWorkflow.run, "work/1/r0/x", id=f"wf-{uuid.uuid4()}", task_queue="store"
            )
    assert len(_ATTEMPTS) == 1
    cause = info.value.cause
    assert isinstance(cause, ActivityError)
    app = cause.cause
    assert isinstance(app, ApplicationError)
    assert app.type == "ArtifactMissingError"
    assert list(app.details) == ["localize"]


@pytest.mark.live
def test_live_minio_round_trip_with_the_localization_credentials() -> None:
    settings = Settings()
    if not settings.s3_endpoint or settings.loc_s3_access_key_id is None:
        pytest.skip("S3_ENDPOINT and LOC_S3_* are not set")
    live = ObjectStore.from_settings(settings)
    key = work_key("999999999", 0, f"probe-{uuid.uuid4()}.txt")
    live.put_bytes(key, b"ok")
    assert live.get_bytes(key, produced_by="probe") == b"ok"
    with pytest.raises(ArtifactMissingError):
        live.get_bytes(key + ".absent", produced_by="probe")

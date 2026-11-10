import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from botocore.exceptions import ClientError

from localization.storage.object_store import StorageConflictError


def test_immutable_collision_preserves_bytes(review_store):
    review_store.put_immutable_bytes("review/7001/g1/rabc/manifest.json", b"first")
    review_store.put_immutable_bytes("review/7001/g1/rabc/manifest.json", b"first")
    with pytest.raises(ValueError, match="checksum mismatch"):
        review_store.put_immutable_bytes("review/7001/g1/rabc/manifest.json", b"second")
    assert (
        review_store.get_bytes("review/7001/g1/rabc/manifest.json", produced_by="test") == b"first"
    )


def test_two_writers_and_stale_head_leave_published_bytes_untouched(review_store):
    key = "review/7001/g1/head.json"

    def write(value):
        try:
            return review_store.compare_and_swap(key, value, None)
        except StorageConflictError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(write, [b"one", b"two"]))
    assert sum(x is not None for x in results) == 1
    _, etag = review_store.read_version(key)
    review_store.compare_and_swap(key, b"edited", etag)
    with pytest.raises(StorageConflictError):
        review_store.compare_and_swap(key, b"lost update", etag)
    assert review_store.read_version(key)[0] == b"edited"


def test_conditional_multipart_reuses_only_identical_bytes(review_store, tmp_path):
    path = tmp_path / "large.mp4"
    path.write_bytes(b"x" * (9 * 1024 * 1024))
    key = "review/7001/g1/assets/large.mp4"
    digest = review_store.put_immutable_file(key, path)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    review_store.put_immutable_file(key, path)
    path.write_bytes(b"y" * (9 * 1024 * 1024))
    with pytest.raises(ValueError, match="checksum mismatch"):
        review_store.put_immutable_file(key, path)
    review_store.verify_file(key, digest)


def test_source_replacement_between_head_and_get_is_refused(review_store, monkeypatch):
    review_store.put_bytes("source/7001.mp4", b"original")
    client = review_store._client
    original = client.get_object

    def replace(**kwargs):
        if kwargs["Key"] == "source/7001.mp4":
            review_store.put_bytes("source/7001.mp4", b"replacement")
        return original(**kwargs)

    monkeypatch.setattr(client, "get_object", replace)
    with pytest.raises(ClientError) as exc:
        review_store.capture_source("source/7001.mp4", "work/7001/g1/r0/source.mp4")
    assert exc.value.response["Error"]["Code"] == "PreconditionFailed"
    assert not review_store.exists("work/7001/g1/r0/source.mp4")


def test_capture_retry_keeps_original_snapshot_after_source_replacement(review_store):
    review_store.put_bytes("source/7001.mp4", b"original")
    first = review_store.capture_source("source/7001.mp4", "work/7001/g1/r0/source.mp4")
    review_store.put_bytes("source/7001.mp4", b"new")
    assert review_store.capture_source("source/7001.mp4", first.snapshot_key) == first
    assert review_store.get_bytes(first.snapshot_key, produced_by="test") == b"original"


def test_conflict_response_does_not_retry_as_unconditional_write(review_store, monkeypatch):
    key = "review/7001/g1/head.json"
    etag = review_store.compare_and_swap(key, b"published", None)

    def conflict(**kwargs):
        assert kwargs["IfMatch"] == etag
        raise ClientError({"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject")

    monkeypatch.setattr(review_store._client, "put_object", conflict)
    with pytest.raises(StorageConflictError):
        review_store.compare_and_swap(key, b"candidate", etag)
    assert review_store.read_version(key)[0] == b"published"


def test_same_revision_two_writers_only_identical_bytes_can_be_reused(review_store):
    key = "review/7001/g1/rabc/manifest.json"

    def commit(data):
        try:
            review_store.put_immutable_bytes(key, data)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(commit, [b"one", b"two"]))
    assert sorted(result) == [False, True]
    assert review_store.read_version(key)[0] in (b"one", b"two")

"""Exact policies from infra/minio/init.sh against an isolated real MinIO; no production
identity is changed."""

import json
import os
import re
import secrets
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[4]
IMAGE = (
    "cgr.dev/chainguard/minio@sha256:"
    "268d68a2a3b6fc65f905a61f84d7eab6345ce6bff14f551e2b028ec31e6a1341"
)
NAME = "reup-review-policy-test"
PORT = 18444


def _policy(name: str) -> str:
    source = (ROOT / "infra/minio/init.sh").read_text()
    match = re.search(rf"write_policy {re.escape(name)} <<JSON\n(.*?)\nJSON", source, re.S)
    assert match is not None
    policy = match[1].replace("$BUCKET_ARN", "arn:aws:s3:::reup-media")
    json.loads(policy)
    return policy


@contextmanager
def isolated_minio(policy_name: str) -> Iterator[tuple]:
    """A throwaway MinIO with bucket reup-media and one user holding exactly the named
    policy from init.sh. Yields (admin client, user client)."""
    # Fail fast if something listens on the port. SO_REUSEADDR lets the bind through
    # TIME_WAIT left by a previous test's connections (~30 s on macOS) but not past a
    # live listener; the wait covers a previous MinIO still shutting down.
    for _ in range(100):
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", PORT))
            break
        except OSError:
            time.sleep(0.1)
    else:
        pytest.fail(f"port {PORT} stayed busy")
    policy = _policy(policy_name)
    env = {
        **os.environ,
        "MINIO_ROOT_USER": "sentinel-fake-admin",
        "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
        "POLICY_USER": "sentinel-fake-user",
        "POLICY_PASSWORD": secrets.token_hex(24),
    }
    process = subprocess.Popen(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            NAME,
            "--tmpfs",
            "/data",
            "-p",
            f"127.0.0.1:{PORT}:9000",
            "-e",
            "MINIO_ROOT_USER",
            "-e",
            "MINIO_ROOT_PASSWORD",
            IMAGE,
            "server",
            "/data",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                with urlopen(f"http://127.0.0.1:{PORT}/minio/health/ready", timeout=1):
                    break
            except OSError:
                assert process.poll() is None, "isolated MinIO exited"
                time.sleep(0.1)
        else:
            pytest.fail("isolated MinIO not ready")
        setup = (
            """set -eu
mc alias set fixture http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb fixture/reup-media >/dev/null
mc admin user add fixture "$POLICY_USER" "$POLICY_PASSWORD" >/dev/null
cat >/tmp/policy.json <<'POLICY'
"""
            + policy
            + f"""
POLICY
mc admin policy create fixture {policy_name} /tmp/policy.json >/dev/null
mc admin policy attach fixture {policy_name} --user "$POLICY_USER" >/dev/null
"""
        )
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                f"container:{NAME}",
                "-e",
                "MINIO_ROOT_USER",
                "-e",
                "MINIO_ROOT_PASSWORD",
                "-e",
                "POLICY_USER",
                "-e",
                "POLICY_PASSWORD",
                "--entrypoint",
                "/bin/sh",
                "reup-flow-minio-init:latest",
            ],
            env=env,
            input=setup,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, "isolated identity provisioning failed"

        def client(user, password):
            return boto3.client(
                "s3",
                endpoint_url=f"http://127.0.0.1:{PORT}",
                region_name="us-east-1",
                aws_access_key_id=user,
                aws_secret_access_key=password,
                config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 0}),
            )

        yield (
            client(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"]),
            client(env["POLICY_USER"], env["POLICY_PASSWORD"]),
        )
    finally:
        subprocess.run(["docker", "stop", "-t", "5", NAME], capture_output=True, check=False)
        process.wait(timeout=15)


def _assert_denied(operations) -> None:
    for operation in operations:
        with pytest.raises(ClientError) as denied:
            operation()
        assert denied.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403


@pytest.mark.live
def test_exact_bridge_policy_get_put_scoped_list_and_denials():
    with isolated_minio("review-bridge") as (admin, review):
        bucket, key = "reup-media", "review/7001/g2/head.json"
        first = review.put_object(Bucket=bucket, Key=key, Body=b"one", IfNoneMatch="*")
        review.put_object(Bucket=bucket, Key=key, Body=b"two", IfMatch=first["ETag"])
        assert review.get_object(Bucket=bucket, Key=key)["Body"].read() == b"two"
        assert review.list_objects_v2(Bucket=bucket, Prefix="review/")["KeyCount"] == 1
        with pytest.raises(ClientError) as stale:
            review.put_object(Bucket=bucket, Key=key, Body=b"stale", IfMatch=first["ETag"])
        assert stale.value.response["ResponseMetadata"]["HTTPStatusCode"] == 412
        operations = [
            lambda: review.delete_object(Bucket=bucket, Key=key),
            lambda: review.list_objects_v2(Bucket=bucket),
        ]
        for prefix in ("source", "work", "cache", "output"):
            outside = f"{prefix}/sentinel-fake-fixture"
            admin.put_object(Bucket=bucket, Key=outside, Body=b"fixture")
            operations.extend(
                [
                    lambda key=outside: review.get_object(Bucket=bucket, Key=key),
                    lambda key=outside: review.put_object(Bucket=bucket, Key=key, Body=b"no"),
                    lambda p=prefix: review.list_objects_v2(Bucket=bucket, Prefix=f"{p}/"),
                ]
            )
        _assert_denied(operations)


@pytest.mark.live
def test_localization_policy_reads_and_writes_output_but_never_deletes_it():
    """The Mac review worker saves a legacy final (output/<id>.mp4) to a collection folder
    with the localization identity, so it must be able to read output/ as well as write it.
    Deleting a published final, or writing the downloader's source/, stays denied."""
    with isolated_minio("localization") as (admin, loc):
        bucket = "reup-media"
        admin.put_object(Bucket=bucket, Key="output/7001.mp4", Body=b"final")
        assert loc.get_object(Bucket=bucket, Key="output/7001.mp4")["Body"].read() == b"final"
        loc.put_object(Bucket=bucket, Key="output/7002.mp4", Body=b"rendered")
        admin.put_object(Bucket=bucket, Key="source/7001.mp4", Body=b"source")
        _assert_denied(
            [
                lambda: loc.delete_object(Bucket=bucket, Key="output/7001.mp4"),
                lambda: loc.put_object(Bucket=bucket, Key="source/7001.mp4", Body=b"no"),
                lambda: loc.list_objects_v2(Bucket=bucket),
            ]
        )

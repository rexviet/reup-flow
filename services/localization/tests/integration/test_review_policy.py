"""Exact bridge policy against isolated real MinIO; no production identity is changed."""

import json
import os
import re
import secrets
import socket
import subprocess
import time
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


@pytest.mark.live
def test_exact_bridge_policy_get_put_scoped_list_and_denials():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", PORT))
    source = (ROOT / "infra/minio/init.sh").read_text()
    match = re.search(r"write_policy review-bridge <<JSON\n(.*?)\nJSON", source, re.S)
    assert match is not None
    policy = match[1].replace("$BUCKET_ARN", "arn:aws:s3:::reup-media")
    json.loads(policy)
    env = {
        **os.environ,
        "MINIO_ROOT_USER": "sentinel-fake-admin",
        "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
        "REVIEW_USER": "sentinel-fake-review",
        "REVIEW_PASSWORD": secrets.token_hex(24),
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
mc admin user add fixture "$REVIEW_USER" "$REVIEW_PASSWORD" >/dev/null
cat >/tmp/review-policy.json <<'POLICY'
"""
            + policy
            + """
POLICY
mc admin policy create fixture review-bridge /tmp/review-policy.json >/dev/null
mc admin policy attach fixture review-bridge --user "$REVIEW_USER" >/dev/null
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
                "REVIEW_USER",
                "-e",
                "REVIEW_PASSWORD",
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

        admin = client(env["MINIO_ROOT_USER"], env["MINIO_ROOT_PASSWORD"])
        review = client(env["REVIEW_USER"], env["REVIEW_PASSWORD"])
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
        for operation in operations:
            with pytest.raises(ClientError) as denied:
                operation()
            assert denied.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
    finally:
        subprocess.run(["docker", "stop", "-t", "5", NAME], capture_output=True, check=False)
        process.wait(timeout=15)

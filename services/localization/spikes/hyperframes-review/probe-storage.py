"""Real isolated MinIO CAS smoke. No production buckets or credentials are used."""

import json
import os
import secrets
import subprocess
import time
from socket import socket
from urllib.request import urlopen

import boto3
import botocore
from botocore.config import Config
from botocore.exceptions import ClientError

IMAGE = (
    "cgr.dev/chainguard/minio@sha256:"
    "268d68a2a3b6fc65f905a61f84d7eab6345ce6bff14f551e2b028ec31e6a1341"
)
NAME = "reup-hyperframes-cas-spike"
PORT = 18443
BUCKET = "hyperframes-review-test"


def main():
    with socket() as probe:
        probe.bind(("127.0.0.1", PORT))
    env = {
        **os.environ,
        "MINIO_ROOT_USER": "fixture-admin",
        "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
        "REVIEW_USER": "fixture-review",
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
        for _ in range(60):
            try:
                with urlopen(f"http://127.0.0.1:{PORT}/minio/health/ready", timeout=1):
                    break
            except OSError:
                if process.poll() is not None:
                    raise RuntimeError("isolated MinIO exited") from None
                time.sleep(0.2)
        else:
            raise RuntimeError("isolated MinIO not ready")
        setup = """set -eu
mc alias set fixture http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb fixture/hyperframes-review-test >/dev/null
mc admin user add fixture "$REVIEW_USER" "$REVIEW_PASSWORD" >/dev/null
cat >/tmp/review-policy.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":["arn:aws:s3:::hyperframes-review-test/review/*"]}]}
EOF
mc admin policy create fixture review-only /tmp/review-policy.json >/dev/null
mc admin policy attach fixture review-only --user "$REVIEW_USER" >/dev/null
"""
        subprocess.run(
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
            check=True,
        )
        client = boto3.client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{PORT}",
            region_name="us-east-1",
            aws_access_key_id=env["REVIEW_USER"],
            aws_secret_access_key=env["REVIEW_PASSWORD"],
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 0}),
        )
        results = {
            "backend": IMAGE,
            "boto3": boto3.__version__,
            "botocore": botocore.__version__,
            "identity": "review-only",
            "bucket": BUCKET,
            "port": PORT,
        }

        def rejected(label, operation, status):
            try:
                operation()
            except ClientError as error:
                actual = error.response["ResponseMetadata"]["HTTPStatusCode"]
                results[label] = actual
                assert actual == status, (label, actual)
            else:
                raise AssertionError(f"{label}: condition incorrectly accepted")

        args = {"Bucket": BUCKET, "Key": "review/HEAD.json"}
        first = client.put_object(**args, Body=b"one", IfNoneMatch="*")
        rejected(
            "duplicate_create",
            lambda: client.put_object(**args, Body=b"other", IfNoneMatch="*"),
            412,
        )
        client.head_object(**args, IfMatch=first["ETag"])
        rejected("head_if_match_stale", lambda: client.head_object(**args, IfMatch='"stale"'), 412)
        client.put_object(**args, Body=b"two", IfMatch=first["ETag"])
        rejected(
            "head_cas_stale",
            lambda: client.put_object(**args, Body=b"lost", IfMatch=first["ETag"]),
            412,
        )
        assert client.get_object(**args)["Body"].read() == b"two"
        results["conditional_head_update"] = "passed"
        rejected(
            "outside_review",
            lambda: client.put_object(Bucket=BUCKET, Key="source/forbidden", Body=b"x"),
            403,
        )
        for label in ("multipart_create", "multipart_duplicate"):
            key = "review/asset.wav"
            upload = client.create_multipart_upload(Bucket=BUCKET, Key=key)["UploadId"]
            part = client.upload_part(
                Bucket=BUCKET, Key=key, UploadId=upload, PartNumber=1, Body=b"audio-fixture"
            )

            def complete(key=key, upload=upload, part=part):
                return client.complete_multipart_upload(
                    Bucket=BUCKET,
                    Key=key,
                    UploadId=upload,
                    MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]},
                    IfNoneMatch="*",
                )

            if label == "multipart_create":
                complete()
                results[label] = "passed"
            else:
                rejected(label, complete, 412)
                client.abort_multipart_upload(Bucket=BUCKET, Key=key, UploadId=upload)
        print(json.dumps(results, indent=2))
    finally:
        subprocess.run(["docker", "stop", "-t", "5", NAME], capture_output=True)
        process.wait(timeout=15)


if __name__ == "__main__":
    main()

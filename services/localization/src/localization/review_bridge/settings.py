"""Bridge settings are deliberately independent from worker/provider settings."""

from pathlib import Path

import boto3
from botocore.config import Config
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from localization.storage.object_store import ObjectStore


class BridgeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="REVIEW_", extra="ignore", hide_input_in_errors=True
    )
    s3_endpoint: str = "http://minio:9000"
    s3_bucket: str = "reup-media"
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr = Field(min_length=3)
    s3_secret_access_key: SecretStr = Field(min_length=16)
    temporal_address: str = "temporal:7233"
    temporal_namespace: str = "default"
    project_root: Path = Path("/review")
    runtime_root: Path = Path("/run/review-bridge")

    def store(self):
        client = boto3.client(
            "s3",
            endpoint_url=self.s3_endpoint,
            region_name=self.s3_region,
            aws_access_key_id=self.s3_access_key_id.get_secret_value(),
            aws_secret_access_key=self.s3_secret_access_key.get_secret_value(),
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
        )
        return ObjectStore(client, self.s3_bucket)

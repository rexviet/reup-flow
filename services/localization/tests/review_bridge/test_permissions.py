import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]


def test_bridge_compose_is_loopback_review_identity_only():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    bridge = compose["services"]["review-bridge"]
    assert bridge["ports"] == ["127.0.0.1:3310:3310"]
    assert bridge["profiles"] == ["review"]
    assert all(name.startswith("REVIEW_") for name in bridge["environment"])
    for word in ["LOC_S3", "DATABASE", "VOICESTUDIO", "OPENROUTER", "VBEE", "MINIO_ROOT"]:
        assert word not in " ".join(bridge["environment"])
    assert not any("docker.sock" in volume for volume in bridge["volumes"])
    assert bridge["read_only"] and bridge["cap_drop"] == ["ALL"]


def test_review_policy_has_no_delete_or_other_prefixes():
    source = (ROOT / "infra/minio/init.sh").read_text()
    raw = source.split("write_policy review-bridge <<JSON\n")[1].split("\nJSON")[0]
    raw = raw.replace("$BUCKET_ARN", "arn:aws:s3:::reup-media")
    statements = json.loads(raw)["Statement"]
    actions = {action for statement in statements for action in statement["Action"]}
    assert actions == {"s3:GetObject", "s3:PutObject", "s3:ListBucket"}
    assert statements[0]["Resource"] == ["arn:aws:s3:::reup-media/review/*"]
    assert statements[1]["Condition"]["StringLike"]["s3:prefix"] == ["review/*", "review/"]

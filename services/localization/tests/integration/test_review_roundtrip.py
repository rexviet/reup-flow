"""Explicit live gate: dedicated review fixture, deployed workers, bridge and safe Studio.

The selected fixture is intentionally edited and revoiced. Never point it at an operator's
in-progress review. Prerequisites and commands are in docs/review-companion.md.
"""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[4]
ORIGIN = "http://127.0.0.1:3310"


def launch(aweme, generation):
    # Captured pipe only: the grant is never placed in argv or printed.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "review-bridge",
            "python",
            "-m",
            "localization.review_bridge.cli",
            "launch",
            aweme,
            generation,
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return json.loads(result.stdout)["grant"]


@pytest.mark.live
async def test_real_bridge_provider_studio_save_revoice_cold_open():
    required = [
        "REVIEW_LIVE_AWEME",
        "REVIEW_LIVE_GENERATION",
        "REVIEW_LIVE_CUE",
        "REVIEW_LIVE_TEXT",
    ]
    if os.environ.get("REVIEW_LIVE_DEDICATED_FIXTURE") != "1" or any(
        not os.environ.get(name) for name in required
    ):
        pytest.skip(
            "Requires explicit dedicated live review fixture, workers/provider and safe Studio"
        )
    aweme, generation, cue_id, text = [os.environ[name] for name in required]
    from localization.review_bridge.models import RevoiceCue
    from localization.storage.object_store import project_head_key

    project_head_key(aweme, int(generation))
    cue_id = int(cue_id)
    # Reject unsafe native Studio before sending it any new project material.
    async with httpx.AsyncClient() as probe:
        for headers in [{"Host": "evil.invalid"}, {"Origin": "https://evil.invalid"}]:
            response = await probe.get("http://127.0.0.1:3002/api/projects", headers=headers)
            assert response.status_code in {400, 403, 421}, "Native Studio boundary gate failed"
    async with httpx.AsyncClient(
        base_url=ORIGIN, headers={"Origin": ORIGIN}, timeout=120
    ) as client:
        redeemed = await client.post("/api/redeem", json={"grant": launch(aweme, generation)})
        assert redeemed.status_code == 200
        headers = {"X-Review-CSRF": redeemed.json()["csrf"]}
        before_response = await client.get("/api/project")
        assert before_response.status_code == 200
        before = before_response.json()
        cue = next(c for c in before["cues"] if c["id"] == cue_id)
        saved_response = await client.put(
            f"/api/cues/{cue_id}",
            json={
                "expectedRevision": before["revision"],
                "headETag": before["headETag"],
                "text": text,
                "start_ms": cue["start_ms"],
                "end_ms": cue["end_ms"],
            },
            headers=headers,
        )
        assert saved_response.status_code == 200
        saved = saved_response.json()
        command = RevoiceCue(
            requestId=str(uuid.uuid4()), cueId=cue_id, baseRevision=saved["revision"]
        )
        for _ in range(2):  # lost response/AlreadyStarted attaches the same workflow
            accepted = await client.post("/api/revoice", json=command.model_dump(), headers=headers)
            assert accepted.status_code == 202
        for _ in range(600):
            polled = await client.get(f"/api/jobs/{command.requestId}")
            assert polled.status_code == 200
            outcome = polled.json()
            if outcome["status"] not in {"running", "reserved", "accepted"}:
                break
            await asyncio.sleep(1)
        assert outcome["status"] == "completed", "Live revoice did not commit a fitted clip"
        after_response = await client.get("/api/project")
        assert after_response.status_code == 200
        after = after_response.json()

        def others(project):
            return [c for c in project["cues"] if c["id"] != cue_id]

        assert others(after) == others(saved)
        changed = next(c for c in after["cues"] if c["id"] == cue_id)
        assert changed["spoken_text"] == text and not changed["mismatch"]
        assert 1 <= changed["stretch_ratio"] <= 1.15
        exported = await client.get("/api/srt")
        assert text in exported.text
        # A trusted launch reads HEAD and rotates the session; it never commits HEAD again.
        reopened = await client.post("/api/redeem", json={"grant": launch(aweme, generation)})
        assert reopened.status_code == 200
        latest = await client.get("/api/project")
        assert latest.status_code == 200 and latest.json()["revision"] == after["revision"]

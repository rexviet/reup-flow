from contextlib import asynccontextmanager

import httpx

from localization.review_bridge.auth import Auth
from localization.review_bridge.server import ORIGIN, create_app


@asynccontextmanager
async def client_for(companion, tmp_path):
    auth = Auth(tmp_path / "auth")
    grant = auth.issue("7001", 1)
    app = create_app(companion, auth)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN)
    async with client:
        response = await client.post(
            "/api/redeem", json={"grant": grant}, headers={"Origin": ORIGIN}
        )
        assert response.status_code == 200
        yield client, response.json()["csrf"], grant


async def test_session_csrf_exact_origin_and_payload_literal(companion, tmp_path):
    async with client_for(companion, tmp_path) as (client, csrf, grant):
        response = await client.get("/api/project")
        assert response.status_code == 200
        state = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert response.headers["referrer-policy"] == "no-referrer"
        for headers in [
            {"Host": "evil.test:3310"},
            {"Origin": "http://127.0.0.1:3002"},
            {"Origin": "https://evil.test"},
            {"Sec-Fetch-Site": "cross-site"},
        ]:
            assert (await client.get("/api/project", headers=headers)).status_code == 403
        command = {
            "expectedRevision": state["revision"],
            "headETag": state["headETag"],
            "text": '</script><img src=x onerror="alert(1)">',
            "start_ms": 0,
            "end_ms": 900,
        }
        assert (
            await client.put("/api/cues/17", json=command, headers={"Origin": ORIGIN})
        ).status_code == 401
        assert (await client.put("/api/cues/17", json=command)).status_code == 403
        response = await client.put(
            "/api/cues/17", json=command, headers={"Origin": ORIGIN, "X-Review-CSRF": csrf}
        )
        assert response.status_code == 200
        srt = (await client.get("/api/srt")).text
        assert command["text"] in srt
        preview = (await client.get("/preview")).text
        assert "</script><img" not in preview and "&lt;img" in preview
        assert "/media/" in preview
        assert (
            await client.put(
                "/api/cues/17",
                json=command,
                headers={
                    "Origin": ORIGIN,
                    "X-Review-CSRF": csrf,
                },
            )
        ).status_code == 409
        assert (
            await client.post("/api/redeem", json={"grant": grant}, headers={"Origin": ORIGIN})
        ).status_code == 401


async def test_cookie_flags_body_limit_and_no_path_api(companion, tmp_path):
    auth = Auth(tmp_path / "private")
    app = create_app(companion, auth)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
        assert (await client.get("/api/project")).status_code == 401
        response = await client.post(
            "/api/redeem", json={"grant": auth.issue("7001", 1)}, headers={"Origin": ORIGIN}
        )
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=28800" in cookie
        csrf = response.json()["csrf"]
        headers = {"Origin": ORIGIN, "X-Review-CSRF": csrf, "Content-Type": "application/json"}
        assert (
            await client.put("/api/cues/17", content='"' + "x" * 33000 + '"', headers=headers)
        ).status_code == 413
        assert (await client.get("/media/manifest.json")).status_code == 404
        assert (await client.get("/api/project?key=source/secret.mp4")).json()["aweme_id"] == "7001"
        assert (
            await client.put("/api/cues/17", json={"object_key": "source/x"}, headers=headers)
        ).status_code == 422

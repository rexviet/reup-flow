"""Exact-origin companion API. No arbitrary object keys or paths cross this boundary."""

import asyncio
import base64
import hashlib
import json
import mimetypes
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from localization.review_bridge.auth import Auth, Unauthorized
from localization.review_bridge.models import RevoiceCue, SaveCue
from localization.review_bridge.repository import Conflict, Repository
from localization.review_bridge.service import QueueFull, Service
from localization.review_bridge.settings import BridgeSettings
from localization.storage.object_store import ArtifactMissingError
from localization.temporal.client import connect

AUTHORITY = "127.0.0.1:3310"
ORIGIN = "http://" + AUTHORITY
STATIC = Path(__file__).parent / "static"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
COOKIE = "review_session"
MAX_BODY = 32_768


async def body(request):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise ValueError("JSON body required")
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BODY:
            return None
    return json.loads(content)


class Boundary(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            origins = request.headers.getlist("origin")
            if (
                request.headers.getlist("host") != [AUTHORITY]
                or (origins and origins != [ORIGIN])
                or request.headers.get("sec-fetch-site") == "cross-site"
            ):
                response = JSONResponse({"error": "Forbidden origin"}, status_code=403)
            elif request.method not in {"GET", "HEAD"} and origins != [ORIGIN]:
                response = JSONResponse({"error": "Origin required"}, status_code=403)
            else:
                public = request.url.path in {
                    "/",
                    "/app.js",
                    "/styles.css",
                    "/favicon.ico",
                    "/api/redeem",
                }
                if not public:
                    state = request.app.state.auth.session(request.cookies.get(COOKIE))
                    request.state.identity = state
                    if request.method not in {"GET", "HEAD"} and not secrets.compare_digest(
                        request.headers.get("x-review-csrf", ""), state["csrf"]
                    ):
                        raise Unauthorized("CSRF token required")
                response = await call_next(request)
        except Unauthorized as exc:
            response = JSONResponse({"error": str(exc)}, status_code=401)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": CSP,
            }
        )
        # The generated preview contains its own hash-based script policy. Never serve
        # Studio-supplied HTML in the companion's authenticated origin.
        if request.url.path == "/preview" and response.status_code == 200:
            del response.headers["Content-Security-Policy"]
            response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        return response


def identity(request):
    state = request.state.identity
    return state["aweme_id"], state["generation"]


def create_app(service=None, auth=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is None:
            settings = BridgeSettings()
            app.state.auth = Auth(settings.runtime_root)
            temporal = await connect(settings.temporal_address, settings.temporal_namespace)
            app.state.service = Service(
                Repository(settings.store(), settings.project_root), temporal
            )
        yield

    async def static(request):
        name = {
            "/": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
            "/favicon.ico": "favicon.svg",
        }[request.url.path]
        return FileResponse(STATIC / name)

    async def redeem(request):
        data = await body(request)
        if data is None:
            return JSONResponse({"error": "Body too large"}, status_code=413)
        if not isinstance(data, dict) or set(data) != {"grant"}:
            raise ValueError("Launch grant required")
        cookie, record = request.app.state.auth.redeem(data["grant"])
        response = JSONResponse({"csrf": record["csrf"]})
        response.set_cookie(COOKIE, cookie, httponly=True, samesite="strict", max_age=8 * 3600)
        return response

    async def session(request):
        return JSONResponse({"csrf": request.state.identity["csrf"]})

    async def project(request):
        value = await asyncio.to_thread(request.app.state.service.project, *identity(request))
        return JSONResponse(value)

    async def save(request):
        data = await body(request)
        if data is None:
            return JSONResponse({"error": "Body too large"}, status_code=413)
        command = SaveCue.model_validate(data)
        result = await asyncio.to_thread(
            request.app.state.service.save, *identity(request), request.path_params["cue"], command
        )
        return JSONResponse(result)

    async def srt(request):
        repo = request.app.state.service.repo
        _, manifest, _ = await asyncio.to_thread(repo.current, *identity(request))
        return Response(
            await asyncio.to_thread(repo.read_asset, manifest.srt),
            media_type="application/x-subrip",
            headers={
                "Content-Disposition": 'attachment; filename="captions.srt"',
            },
        )

    async def revoice(request):
        data = await body(request)
        if data is None:
            return JSONResponse({"error": "Body too large"}, status_code=413)
        command = RevoiceCue.model_validate(data)
        return JSONResponse(
            await request.app.state.service.revoice(*identity(request), command), status_code=202
        )

    async def job(request):
        return JSONResponse(
            await request.app.state.service.job(
                *identity(request), request.path_params["request_id"]
            )
        )

    async def refresh(request):
        project, _ = await asyncio.to_thread(
            request.app.state.service.repo.materialize, *identity(request)
        )
        return JSONResponse({"revision": project.revision_id})

    async def preview(request):
        repo = request.app.state.service.repo
        _, manifest, _ = await asyncio.to_thread(repo.current, *identity(request))
        source = (await asyncio.to_thread(repo.read_asset, manifest.html)).decode()
        # Relative media resolve within /media/; no user input enters this rewrite.
        assets = [manifest.video, manifest.mixed_audio, manifest.font]
        for asset in assets:
            name = Path(asset.key).name
            source = source.replace(f'"{name}"', f'"/media/{name}"')
            source = source.replace(f"'{name}'", f"'/media/{name}'")
        # The companion iframe owns playback just as the standalone page does.
        # Recompute only the known generated player's hash, keeping CSP restrictive.
        match = re.search(r"<script>(.*?)</script>", source, re.DOTALL)
        if match:
            original = match.group(1)
            player = original.replace("if(window.parent===window){", "if(true){")
            player += f"""
const composition=document.getElementById('review');
function fitPreview(){{
 const scale=Math.min(innerWidth/{manifest.width},innerHeight/{manifest.height});
 composition.style.transformOrigin='top left';
 composition.style.transform='scale('+scale+')';
 composition.style.position='absolute';
 composition.style.left=((innerWidth-{manifest.width}*scale)/2)+'px';
 composition.style.top=((innerHeight-{manifest.height}*scale)/2)+'px';
 document.body.style.overflow='hidden';
}}
addEventListener('resize',fitPreview);fitPreview();
"""
            old_hash = base64.b64encode(hashlib.sha256(original.encode()).digest()).decode()
            new_hash = base64.b64encode(hashlib.sha256(player.encode()).digest()).decode()
            source = source.replace(original, player).replace(old_hash, new_hash)
        return Response(source, media_type="text/html")

    async def media(request):
        repo = request.app.state.service.repo
        _, manifest, _ = await asyncio.to_thread(repo.current, *identity(request))
        name = request.path_params["name"]
        asset = next(
            (
                a
                for a in (manifest.video, manifest.mixed_audio, manifest.font)
                if Path(a.key).name == name
            ),
            None,
        )
        if asset is None:
            return Response(status_code=404)
        # FileResponse supports media ranges and avoids buffering the video for each seek.
        folder = repo.folder(*identity(request))
        from localization.review_bridge.repository import atomic_write, safe_path

        path = safe_path(folder, name)

        def verified():
            if not path.exists():
                return False
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest() == asset.sha256

        if not await asyncio.to_thread(verified):
            atomic_write(path, await asyncio.to_thread(repo.read_asset, asset))
        return FileResponse(path, media_type=mimetypes.guess_type(name)[0])

    async def error(request, exc):
        if isinstance(exc, QueueFull):
            return JSONResponse({"error": str(exc)}, status_code=429)
        if isinstance(exc, Conflict):
            return JSONResponse({"error": str(exc), "revision": exc.revision}, status_code=409)
        if isinstance(exc, Unauthorized):
            return JSONResponse({"error": str(exc)}, status_code=401)
        if isinstance(exc, ArtifactMissingError):
            return JSONResponse({"error": "Review artifact unavailable"}, status_code=404)
        # Never serialize raw validation inputs or backend/provider errors.
        return JSONResponse({"error": "Invalid command or cue timing"}, status_code=422)

    app = Starlette(
        routes=[
            Route("/", static),
            Route("/app.js", static),
            Route("/styles.css", static),
            Route("/favicon.ico", static),
            Route("/api/redeem", redeem, methods=["POST"]),
            Route("/api/session", session),
            Route("/api/project", project),
            Route("/api/cues/{cue:int}", save, methods=["PUT"]),
            Route("/api/srt", srt),
            Route("/api/revoice", revoice, methods=["POST"]),
            Route("/api/jobs/{request_id}", job),
            Route("/api/refresh", refresh, methods=["POST"]),
            Route("/preview", preview),
            Route("/media/{name}", media),
        ],
        lifespan=lifespan,
        exception_handlers={
            Conflict: error,
            QueueFull: error,
            Unauthorized: error,
            ValueError: error,
            ValidationError: error,
            ArtifactMissingError: error,
        },
    )
    app.state.service = service
    app.state.auth = auth
    app.add_middleware(Boundary)
    return app


def main():
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=3310,
        access_log=False,
        proxy_headers=False,
        limit_concurrency=32,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()

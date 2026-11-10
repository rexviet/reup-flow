"""Provider-free real HTTP/Temporal/media fixture for companion browser checks.

Runs only as an explicit test utility, on the same deterministic port as production.
The caller must check ownership before starting it. No production data or credentials.
"""

import asyncio
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import boto3
import uvicorn
from moto import mock_aws
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from localization.activities.ffmpeg import run_ffmpeg
from localization.config import DEFAULT_CAPTION_FONT
from localization.review_bridge.auth import Auth
from localization.review_bridge.server import create_app
from localization.storage.object_store import ObjectStore
from localization.temporal.client import connect
from localization.workflows.revoice_caption import RevoiceCaptionWorkflow
from tests.support.revoice import audio_project
from tests.support.tts_fakes import make_wav

ROOT = Path(__file__).resolve().parents[4] / "data" / "phase04-ui"


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    with mock_aws(), tempfile.TemporaryDirectory(prefix="review-browser-") as directory:
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="review-browser")
        store = ObjectStore(client, "review-browser")
        folder = Path(directory).resolve()
        service, activities, _ = audio_project(store, folder)
        repository = service.repo
        _, base, etag = repository.current("7001", 1)
        video = folder / "source.mp4"
        run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                "color=c=0x182231:s=640x360:r=25:d=2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ]
        )
        video_asset = repository.asset(base, video.read_bytes(), ".mp4")
        base = base.model_copy(
            update={
                "source": base.source.model_copy(update={"sha256": video_asset.sha256}),
                "video": video_asset,
                "font": repository.asset(base, DEFAULT_CAPTION_FONT.read_bytes(), ".ttf"),
                "mixed_audio": repository.asset(base, make_wav(2, frequency=220), ".wav"),
            }
        )
        repository.activate(repository.prepare(base, base.cues, {}), etag)
        repository.materialize("7001", 1)
        auth = Auth(ROOT / "runtime")
        async with (
            await WorkflowEnvironment.start_local() as environment,
            AsyncExitStack() as stack,
        ):
            temporal = await connect(environment.client.service_client.config.target_host)
            service.temporal = temporal
            for queue, functions in [
                ("workflows", []),
                ("tts", [activities.synthesize]),
                ("media", [activities.mix]),
                ("io", [activities.commit, activities.failed]),
            ]:
                await stack.enter_async_context(
                    Worker(
                        temporal,
                        task_queue=queue,
                        activities=functions,
                        workflows=[RevoiceCaptionWorkflow] if queue == "workflows" else [],
                    )
                )
            print(
                "Companion browser fixture ready: port 3310; synthetic test media only", flush=True
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    create_app(service, auth),
                    host="127.0.0.1",
                    port=3310,
                    access_log=False,
                    log_level="warning",
                )
            )
            await server.serve()


if __name__ == "__main__":
    asyncio.run(main())

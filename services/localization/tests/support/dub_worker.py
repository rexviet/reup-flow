"""A worker process the resume test can kill.

    python -m tests.support.dub_worker <temporal address> <state dir> <mode> <tts limit>

It serves the real workflow, the real synthesis and caching, on a bucket that is a
directory (so the test can look at it) and a provider that appends every call to a
log file. Modes:

  stall      the provider stalls, once, when two full batches are cached and it is
             about to be asked for the next clip: nothing is in flight, so the test
             can kill the process at a moment that costs no repeated call.
  killput    the first time a clip is about to be written after `KILL_AFTER` clips
             are cached, the process kills itself between the provider's answer and
             the write.
  run        no fault.

`state dir/calls.log` has one line per provider call (the text). `state dir/marker`
exists once the fault has been injected (and holds the text of the clip it hit).
"""

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from localization.contracts.voice import VoiceRef
from localization.storage.object_store import ObjectStore
from localization.temporal.client import connect
from tests.support.file_s3 import FileS3
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers
from tests.support.real_dubbing import RealDubbing
from tests.support.tts_fakes import make_wav

CLIPS_PER_TWO_BATCHES = 20
KILL_AFTER = 24
BUCKET = "reup-media"


class LoggingTts:
    def __init__(self, state: Path, s3: FileS3, mode: str) -> None:
        self._state = state
        self._s3 = s3
        self._mode = mode

    def _cached(self) -> int:
        return len(self._s3.keys("cache/tts/"))

    async def synthesize(self, text: str, voice: VoiceRef) -> bytes:
        marker = self._state / "marker"
        if (
            self._mode == "stall"
            and not marker.exists()
            and self._cached() >= CLIPS_PER_TWO_BATCHES
        ):
            marker.write_text(text)
            await asyncio.sleep(600)
        # One write, appended: a line is never torn by a kill.
        fd = os.open(self._state / "calls.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.write(fd, (text + "\n").encode())
        os.close(fd)
        await asyncio.sleep(0.05)
        return make_wav(0.5)

    def before_put(self, digest: str) -> None:
        marker = self._state / "marker"
        if self._mode == "killput" and not marker.exists() and self._cached() >= KILL_AFTER:
            marker.write_text(digest)
            os.kill(os.getpid(), signal.SIGKILL)
            time.sleep(60)


async def main(address: str, state_dir: str, mode: str, limit: str) -> None:
    state = Path(state_dir)
    s3 = FileS3(state / "bucket")
    store = ObjectStore(s3, BUCKET)
    tts = LoggingTts(state, s3, mode)
    client = await connect(address)
    dubbing = RealDubbing(store, tts, before_put=tts.before_put)
    async with prep_workers(client, PrepFakes(), dubbing=dubbing, tts_limit=int(limit)):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:5]))

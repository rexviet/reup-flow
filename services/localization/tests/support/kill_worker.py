"""A worker process the kill test can SIGKILL.

    python -m tests.support.kill_worker <temporal address> <database conninfo> <marker file>

It serves the real workflow and the real Localizer with a scripted model, and a
`glossary_upsert` that writes to the real database and then, the first time only
(the marker file records that it has happened), stalls: that is the moment the test
kills the process, after the row is committed and before the tool can answer.
"""

import asyncio
import sys
from pathlib import Path

from localization.activities.glossary import GlossaryStore
from localization.agents.localizer import SaveResult
from localization.temporal.client import connect
from tests.support.localizer_fakes import FakePorts, LocalizerFakes, ScriptedModel
from tests.support.prep_fakes import PrepFakes
from tests.support.prep_worker import prep_workers

TERM = ("苹果", "Apple")


class StallsAfterTheFirstWrite(FakePorts):
    def __init__(self, store: GlossaryStore, marker: Path) -> None:
        super().__init__()
        self._store = store
        self._marker = marker

    async def save_term(self, **kw) -> SaveResult:  # type: ignore[override]
        outcome = await self._store.upsert(**kw)
        if not self._marker.exists():
            self._marker.write_text("written")
            await asyncio.sleep(600)
        return SaveResult(outcome.status, outcome.message, outcome.existing)


async def main(address: str, conninfo: str, marker: str) -> None:
    client = await connect(address)
    localizing = LocalizerFakes(
        ports=StallsAfterTheFirstWrite(GlossaryStore(conninfo), Path(marker)),
        model=ScriptedModel(
            tool_calls=(
                ("read_transcript", {}),
                ("glossary_upsert", {"term_zh": TERM[0], "term_vi": TERM[1]}),
            )
        ),
    )
    async with prep_workers(client, PrepFakes(), localizing):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:4]))

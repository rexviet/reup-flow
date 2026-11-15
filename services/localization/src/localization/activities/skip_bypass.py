"""Whether the operator set a run's skip checks aside.

The dashboard sets `localization_outbox.bypass_skips` when it goes on from a run that a skip
check ended (a video judged an advertisement, say); the starter resets the run to that check.
A skip check asks here first and, when the run's skips are set aside, lets the video through.
The flag speaks only for the generation the outbox row holds: a run of another generation is
never let through by it.
"""

import psycopg

BYPASS_SQL = """
SELECT bypass_skips FROM localization_outbox WHERE aweme_id = %s AND generation = %s
"""


class SkipBypasses:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    async def __call__(self, aweme_id: str, generation: int) -> bool:
        async with await psycopg.AsyncConnection.connect(self._conninfo, autocommit=True) as conn:
            row = await (await conn.execute(BYPASS_SQL, (aweme_id, generation))).fetchone()
        return bool(row and row[0])

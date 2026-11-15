"""Live probe of Jev through OpenRouter: which base URL and model id work, what usage looks like."""

import asyncio
import sys
from pathlib import Path

from dotenv import dotenv_values
from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy

ENV = Path(__file__).resolve().parents[3] / ".env"


async def main(base_url: str, model: str) -> None:
    key = dotenv_values(ENV)["OPENROUTER_API_KEY"] or ""
    client = AsyncTypeSafeClient(
        api_key=key, base_url=base_url, model=model, retry=RetryPolicy(max_retries=0), timeout=30
    )
    try:
        r = await client.system_one(
            state={"source_zh": "转发", "target_vi": "Chia sẻ"},
            questions={"ok": Noul(instructions="target_vi is a faithful translation of source_zh")},
        )
        print(base_url, model, "->", r.model_dump())
    except Exception as e:  # noqa: BLE001
        print(base_url, model, "FAILED", type(e).__name__, str(e).replace(key, "***")[:300])


asyncio.run(main(sys.argv[1], sys.argv[2]))

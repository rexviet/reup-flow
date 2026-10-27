"""Calls the real Jev through OpenRouter on the lines of a real run (a few cents at most).
Run by hand, with OPENROUTER_API_KEY in the repo's .env:

uv run pytest tests/providers/test_live_jev.py -m live

The lines are the ones of clip 7575765838708520689: the Localizer dropped `与点点栏目`
from line 2 in four generations, and one version kept both names.
"""

from pathlib import Path

import pytest
from dotenv import dotenv_values

from localization.agents import review_jev
from localization.config import Settings
from localization.contracts import LocalizedSegment
from localization.providers.jev import jev_for

pytestmark = pytest.mark.live


def seg(i: int, zh: str, vi: str) -> LocalizedSegment:
    return LocalizedSegment(
        id=i, start_ms=0, end_ms=100, slot_ms=1000, src_zh=zh, text_vi=vi, on_screen=False
    )


async def test_jev_separates_the_real_dropped_name_from_faithful_lines() -> None:
    # The suite clears the settings' variables, so the key is read from the repo's .env.
    key = dotenv_values(Path(__file__).resolve().parents[4] / ".env").get("OPENROUTER_API_KEY")
    if not key:
        pytest.skip("OPENROUTER_API_KEY is not in the repo's .env")
    settings = Settings(openrouter_api_key=key)  # type: ignore[arg-type]
    support = "打赏支持明镜与点点栏目"
    lines = [
        seg(0, "请不吝点赞订阅", "Xin đừng tiếc một lượt thích và đăng ký"),
        seg(1, "转发", "Chia sẻ nhé"),
        seg(2, support, "Ủng hộ kênh đi"),
        seg(3, support, "Ủng hộ chuyên mục"),
        seg(4, support, "Ủng hộ Minh Kính và Điểm Điểm nhé"),
        seg(5, "请不吝点赞", "Hãy like nhé"),
    ]
    jev = jev_for(settings)
    result = await jev.ask(review_jev.build_state(lines), review_jev.build_questions(lines))
    flagged = {i.segment_id for i in review_jev.fidelity_issues(lines, result.answers).issues}
    assert flagged == {2, 3}

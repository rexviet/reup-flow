"""The Localizer against the real model on OpenRouter. Run by hand with the repo's
.env present:

    uv run pytest tests/agents/test_live.py -m live -s

The transcript below is hand-written Mandarin in the register of a short product
video, with numbers and a Latin brand name. It is NOT a transcript of a real video,
so the numbers it prints say whether the model calls the tools and fits the slots,
and are not the evaluation baseline (that comes from a real run).
"""

import asyncio
from pathlib import Path

import pytest
from dotenv import dotenv_values

from localization.agents.localizer import SegmentView, bind_ports, build_localizer
from localization.config import Settings
from localization.providers.llm import model_for
from localization.text.vi_syllables import estimate_ms
from tests.agents.test_localizer import deps
from tests.support.localizer_fakes import FakePorts

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[4]

SAMPLE = [
    (0, 2400, "大家好，欢迎回到我的频道"),
    (2600, 5800, "今天给大家测评一下新出的iPhone 16"),
    (6000, 9200, "它的价格是5999元，比去年便宜了10%"),
    (9400, 12000, "我已经用了整整15天，先说结论"),
    (12200, 15600, "电池真的很耐用，一天一充完全没问题"),
    (15800, 18400, "拍照效果也非常好，夜景特别清楚"),
    (18600, 21800, "不过它的充电速度还是有点慢"),
    (22000, 24600, "如果你喜欢苹果，这台值得买"),
    (24800, 27200, "好了，今天就到这里，我们下期再见"),
]


# Every model listed as able to call tools, else the configured one.
MODELS = [
    m.strip()
    for m in (dotenv_values(REPO / ".env").get("G1_OPENROUTER_MODELS") or "").split(",")
    if m.strip()
] or [dotenv_values(REPO / ".env").get("LOCALIZER_MODEL") or ""]
# A model that has not answered in this long is not usable for a short video.
BOUND_SECONDS = 240


@pytest.mark.parametrize("model_name", MODELS)
async def test_the_real_model_covers_every_segment_and_calls_the_tools(model_name: str) -> None:
    settings = Settings(_env_file=REPO / ".env")  # type: ignore[call-arg]
    if not (settings.openrouter_api_key and model_name):
        pytest.skip("OPENROUTER_API_KEY and a model are not set")
    settings.localizer_model = model_name
    ports = FakePorts(segment_list=[SegmentView(i, a, b, t) for i, (a, b, t) in enumerate(SAMPLE)])
    bind_ports(ports)
    try:
        agent = build_localizer(model_for("localizer", settings))
        d = deps(len(SAMPLE), syllables_per_sec=4.2)
        result = await asyncio.wait_for(
            agent.run(
                "Localize this video's transcript into Vietnamese. "
                f"The segment ids are 0 to {len(SAMPLE) - 1}.",
                deps=d,
            ),
            BOUND_SECONDS,
        )
    finally:
        bind_ports(None)
    out = {line.id: line.text_vi for line in result.output.lines}
    assert sorted(out) == list(range(len(SAMPLE)))
    assert ports.calls["segments"] >= 1, "the model never read the transcript"
    over = 0
    for i, (a, b, _) in enumerate(SAMPLE):
        estimate, slot = estimate_ms(out[i], 4.2), b - a
        over += estimate > slot * 1.15
        print(f"[{i}] slot {slot} ms, estimate {estimate} ms: {out[i]}")
    print(
        f"{model_name}: over 1.15 x slot: {over}/{len(SAMPLE)} "
        "(hand-written sample, not a baseline)"
    )
    print(f"requests: {result.usage.requests}, tool calls: {result.usage.tool_calls}")

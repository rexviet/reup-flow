"""Live spike: does Jev, through OpenRouter, tell a faithful line from an unfaithful one?

Run: uv run --with typesafe-sdk==0.7.2 python eval/jev_fidelity_spike.py [repeats]
Each case is one system_one call with two Noul questions; probabilities are printed, thresholds
are not applied here. The key is read from the repo .env and is never printed.
"""

import asyncio
import statistics
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy

ENV = Path(__file__).resolve().parents[3] / ".env"
BASE_URL = "https://openrouter.ai/api"
MODEL = "typesafe/jev-1.13"

QUESTIONS = {
    "omits_content": Noul(
        instructions=(
            "`target_vi` leaves out something that `source_zh` says: a person, channel, show, "
            "brand or column name, a number, or a call to action (like, subscribe, share, "
            "donate)."
        ),
        criteria={
            "true": "Something named or asked for in source_zh is missing from target_vi.",
            "false": "Every name, number and call to action in source_zh appears in target_vi, "
            "translated or transliterated.",
        },
    ),
    "drops_clause": Noul(
        instructions=(
            "Walk through `source_zh` clause by clause. `target_vi` is missing a whole clause "
            "or sentence of it, even if what remains reads well."
        ),
        criteria={
            "true": "At least one clause of source_zh has no counterpart in target_vi.",
            "false": "Every clause of source_zh has a counterpart in target_vi.",
        },
    ),
    "changes_meaning": Noul(
        instructions=(
            "`target_vi` says something different from `source_zh`: a wrong action, wrong "
            "number, wrong subject, or text that is not Vietnamese."
        ),
        criteria={
            "true": "target_vi means something other than source_zh.",
            "false": "target_vi means the same as source_zh.",
        },
    ),
}

ZH_SUPPORT = "打赏支持明镜与点点栏目"
LONG_ZH = (
    "你要相信只有坚持了别人无法坚持的坚持才能拥有别人无法拥有的拥有天赋可以让一个人闪闪发光但是努力也能光荣与梦想千篇一立自律与忍耐万里挑一"
    "真正能让你走远的从来都是积极自律与勤奋没有醒不来的早晨弄不懂的题目熬不过的迷茫只有你不敢追的梦"
)
LONG_VI_V1 = (
    "Bạn phải tin rằng chỉ khi kiên trì được điều người khác không thể kiên trì, bạn mới có được "
    "thứ người khác không thể có. Tài năng có thể khiến một người tỏa sáng, nhưng nỗ lực cũng "
    "làm nên vinh quang. Ước mơ ai cũng giống nhau, nhưng kỷ luật và nhẫn nại thì vạn người mới "
    "có một. Điều thực sự đưa bạn đi xa, mãi mãi là tích cực, tự giác và chăm chỉ. Không có buổi "
    "sáng nào không dậy được, không có bài toán nào không hiểu nổi, không có mê mang nào không "
    "vượt qua, chỉ có giấc mơ mà bạn không dám theo đuổi."
)
LONG_VI_V2 = (
    "Hãy tin rằng chỉ khi kiên trì điều người khác bỏ cuộc, bạn mới có được thứ họ không thể có. "
    "Tài năng khiến người ta tỏa sáng, nhưng nỗ lực cũng làm nên vinh quang. Ước mơ ai cũng giống "
    "nhau, kỷ luật và nhẫn nại mới là hiếm có. Điều đưa bạn đi xa mãi mãi là tích cực, tự giác và "
    "chăm chỉ. Không có sáng nào không dậy được, không bài toán nào không hiểu, không mê mang nào "
    "không vượt qua, chỉ có giấc mơ bạn không dám theo đuổi."
)

# (label, expected, source_zh, target_vi, slot_ms)
CASES = [
    ("real g4 line2 'Ung ho chuyen muc'", "bad", ZH_SUPPORT, "Ủng hộ chuyên mục", 1000),
    ("real g6 line2 'Ung ho kenh'", "bad", ZH_SUPPORT, "Ủng hộ kênh", 1000),
    ("real g11/g12 line2 'Ung ho kenh di'", "bad", ZH_SUPPORT, "Ủng hộ kênh đi", 1000),
    ("real good line2 (both names)", "good", ZH_SUPPORT, "Ủng hộ Minh Kính và Điểm Điểm nhé", 1000),
    ("real line0 whole", "good", "请不吝点赞订阅", "Xin đừng tiếc một lượt thích và đăng ký", 2986),
    ("real line 'Chia se nhe'", "good", "转发", "Chia sẻ nhé", 1540),
    ("real line 'Hay like nhe'", "good", "请不吝点赞", "Hãy like nhé", 1445),
    ("real line 'Dang ky kenh'", "good", "订阅", "Đăng ký kênh", 1541),
    ("aweme2 g1 long", "good", LONG_ZH, LONG_VI_V1, 23054),
    ("aweme2 g2 long", "good", LONG_ZH, LONG_VI_V2, 23054),
    ("synth dropped 2nd name", "bad", ZH_SUPPORT, "Ủng hộ Minh Kính nhé", 1000),
    ("synth wrong action (like->sub)", "bad", "请不吝点赞", "Hãy đăng ký nhé", 1445),
    ("synth wrong meaning (share->comment)", "bad", "转发", "Bình luận nhé", 1540),
    ("synth untranslated Chinese", "bad", ZH_SUPPORT, "Ủng hộ 明镜与点点栏目", 1000),
    ("synth empty", "bad", "订阅", "", 1541),
    (
        "synth wrong number",
        "bad",
        "万里挑一，只有十分之一的人能做到",
        "Vạn người mới có một, chỉ có một phần trăm người làm được",
        4000,
    ),
    (
        "synth long dropped last clause",
        "bad",
        LONG_ZH,
        LONG_VI_V2.rsplit(", chỉ có", 1)[0] + ".",
        23054,
    ),
    (
        "control wrong-number source",
        "good",
        "万里挑一，只有十分之一的人能做到",
        "Vạn người mới có một, chỉ có một phần mười người làm được",
        4000,
    ),
]


async def one(client, sem, zh, vi, slot):
    async with sem:
        t0 = time.monotonic()
        r = await client.system_one(
            state={"source_zh": zh, "target_vi": vi, "slot_ms": slot}, questions=QUESTIONS
        )
        return (
            {k: a.noul for k, a in r.answers.items()},
            int((time.monotonic() - t0) * 1000),
            r.usage.input_tokens,
            r.usage.output_tokens,
            r.model,
        )


async def main(repeats: int) -> None:
    key = dotenv_values(ENV)["OPENROUTER_API_KEY"] or ""
    client = AsyncTypeSafeClient(
        api_key=key, base_url=BASE_URL, model=MODEL, retry=RetryPolicy(max_retries=0), timeout=45
    )
    sem = asyncio.Semaphore(4)
    jobs = [one(client, sem, zh, vi, slot) for _, _, zh, vi, slot in CASES for _ in range(repeats)]
    out = await asyncio.gather(*jobs)
    lat, tin, tout = [], 0, 0
    for i, (label, exp, *_rest) in enumerate(CASES):
        runs = out[i * repeats : (i + 1) * repeats]
        om = [r[0]["omits_content"] for r in runs]
        cm = [r[0]["changes_meaning"] for r in runs]
        dc = [r[0]["drops_clause"] for r in runs]
        lat += [r[1] for r in runs]
        tin += sum(r[2] for r in runs)
        tout += sum(r[3] for r in runs)
        fmt = lambda xs: "/".join(f"{x:.2f}" for x in xs)  # noqa: E731
        print(f"{exp:4} | omits {fmt(om)} | clause {fmt(dc)} | meaning {fmt(cm)} | {label}")
    print(f"latency ms median {statistics.median(lat)} max {max(lat)}; tokens in {tin} out {tout}")
    print("model ids:", {r[4] for r in out})


asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))

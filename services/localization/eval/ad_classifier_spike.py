"""Can Jev tell a product advertisement from the transcript alone?

    uv run python eval/ad_classifier_spike.py transcripts.json

`transcripts.json` maps an aweme id to a list of `{"id": n, "text": "..."}` lines, the
speech-to-text answer for that video. One Jev `system_one` call per video carries the whole
transcript and a few yes/no questions; Jev answers each with a probability. The script prints
the probabilities and what a threshold on the first question would decide, so the questions and
the threshold can be judged against videos whose kind is known.
"""

import asyncio
import json
import sys
import time

from common import load_env
from typesafe_sdk import Noul

from localization.config import Settings
from localization.providers.jev import jev_for

THRESHOLD = 0.8

QUESTIONS = {
    "promotes_product": Noul(
        instructions=(
            "Read `transcript`, the speech of a short video. Decide whether the speaker "
            "promotes a specific product or brand to persuade viewers to buy or use it: "
            "recommending it, praising it, or listing its features or benefits."
        ),
        criteria={
            "true": "The speech recommends or praises a specific product or brand, or lists its "
            "benefits or features, in order to sell it.",
            "false": "The speech does not sell a product. It is a story, a tutorial, an opinion, "
            "song lyrics, news, a motivational talk, or a request to like, follow or support a "
            "channel.",
        },
    ),
    "sales_language": Noul(
        instructions=(
            "Read `transcript`. Decide whether it uses selling language: asking viewers to buy, "
            "a price or discount, a shop, a link, a coupon or another way to purchase."
        ),
        criteria={
            "true": "The speech asks viewers to buy or gives a price, discount, shop, link or "
            "coupon.",
            "false": "The speech has no purchase request, price, discount, shop, link or coupon.",
        },
    ),
    "names_a_brand": Noul(
        instructions="Read `transcript`. Decide whether it names a specific commercial brand or product.",
        criteria={
            "true": "A specific commercial brand or product is named.",
            "false": "No specific commercial brand or product is named.",
        },
    ),
}


async def classify(jev, lines: list[dict]) -> tuple[dict[str, float], float]:
    began = time.perf_counter()
    result = await jev.ask({"transcript": lines}, QUESTIONS)
    return result.answers, time.perf_counter() - began


async def main() -> int:
    load_env()
    jev = jev_for(Settings())
    data = json.load(open(sys.argv[1]))
    spoken = {a: lines for a, lines in data.items() if isinstance(lines, list) and lines}
    print(f"{len(spoken)} video(s) with speech; threshold {THRESHOLD} on promotes_product\n")
    out = await asyncio.gather(*(classify(jev, lines) for lines in spoken.values()), return_exceptions=True)
    print(f"{'video':8} {'segs':>4} {'promotes':>9} {'sales':>6} {'brand':>6} {'secs':>5}  decision   first words")
    for (aweme, lines), res in zip(spoken.items(), out, strict=True):
        text = " ".join(x["text"] for x in lines)[:44].replace("\n", " ")
        if isinstance(res, Exception):
            print(f"{aweme[-6:]:8} {len(lines):4d} ERROR {type(res).__name__}: {str(res)[:80]}")
            continue
        a, secs = res
        decision = "SKIP (ad)" if a.get("promotes_product", 0) >= THRESHOLD else "process"
        print(f"{aweme[-6:]:8} {len(lines):4d} {a.get('promotes_product', float('nan')):9.2f} "
              f"{a.get('sales_language', float('nan')):6.2f} {a.get('names_a_brand', float('nan')):6.2f} "
              f"{secs:5.1f}  {decision:10s} {text}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

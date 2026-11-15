"""What Jev is asked about a video's speech before it is localized, and what its answer means.

A product advertisement is worth something only to the market it was made for: a dubbed one
sells nothing here. Jev reads the whole transcript and answers one Noul question,
`promotes_product`, with the probability that the speaker sells a product; at or over the
threshold the video is not localized.

Measured on real transcripts through OpenRouter: two product advertisements (an iron
supplement, an anti hair loss shampoo) scored 0.99, and six videos that sell nothing (a
channel's like-and-subscribe request, health tips, a motivational talk, English song lyrics)
scored 0.01 to 0.05. Other questions asked beside it (selling language, a named brand) did
not separate the two kinds: one advertisement scored 0.09 on selling language. So the
decision is this one question.

The transcript is untrusted (it is the video's own speech). Jev's output is one
probability, so there is nothing in it for an injected instruction to steer but that number.
"""

from collections.abc import Sequence

from typesafe_sdk import Noul

from localization.contracts import SttSegment

QUESTION = "promotes_product"

QUESTIONS = {
    QUESTION: Noul(
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
}


def build_state(segments: Sequence[SttSegment]) -> dict[str, object] | None:
    """The transcript as Jev reads it, or None when nothing is said: a video without speech
    is not classified."""
    lines = [{"id": s.id, "text": s.text.strip()} for s in segments if s.text.strip()]
    return {"transcript": lines} if lines else None


def is_advertisement(answers: dict[str, float], threshold: float) -> bool:
    return answers[QUESTION] >= threshold

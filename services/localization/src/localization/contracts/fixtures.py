"""Sample `EditSpec` documents for the renderer's contract test.

python -m localization.contracts.fixtures OUT_DIR

Writes `<name>.json` (what the layout stage stores) and `<name>.sha256` (the hash the
renderer must compute for it) for each sample. The renderer's tests regenerate them
from the Pydantic model on every run, so a model change that the renderer's schema
does not follow is a failing test and not a surprise in production.
"""

import sys
from pathlib import Path

from localization.contracts.models import EditSpec
from localization.contracts.spec_hash import spec_sha256

_AUDIO = "a" * 64


def _base(**over: object) -> dict:
    spec = {
        "source_key": "work/7001/r0/source.mp4",
        "audio_key": "work/7001/r1/dubbed.wav",
        "audio_sha256": _AUDIO,
        "script_version": 2,
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "duration_ms": 3000,
        "cover": None,
        "caption_box": {"x": 54, "y": 1498, "w": 972, "h": 269},
        "captions": [],
        "font": "BeVietnamPro-SemiBold.ttf",
    }
    spec.update(over)
    return spec


def samples() -> dict[str, EditSpec]:
    return {
        "with-cover": EditSpec.model_validate(
            _base(
                cover={
                    "region": {"x": 40, "y": 1500, "w": 1000, "h": 120},
                    "color": "#101010",
                    "opacity_pct": 92,
                },
                caption_box={"x": 40, "y": 1500, "w": 1000, "h": 120},
                captions=[
                    {
                        "segment_id": 0,
                        "start_ms": 0,
                        "end_ms": 1500,
                        "text": "Xin chào các bạn",
                        "font_px": 48,
                    },
                    {
                        "segment_id": 1,
                        "start_ms": 1500,
                        "end_ms": 3000,
                        "text": "Hôm nay chúng ta học\ncách nấu một nồi phở",
                        "font_px": 40,
                    },
                ],
            )
        ),
        "no-cover": EditSpec.model_validate(
            _base(
                captions=[
                    {
                        "segment_id": 0,
                        "start_ms": 500,
                        "end_ms": 2500,
                        "text": "Không có phụ đề cũ",
                        "font_px": 48,
                    }
                ]
            )
        ),
        "awkward-text": EditSpec.model_validate(
            _base(
                captions=[
                    {
                        "segment_id": 0,
                        "start_ms": 0,
                        "end_ms": 3000,
                        # quotes, a backslash, an emoji, and the same letter written
                        # precomposed and decomposed: the hash covers the exact bytes.
                        "text": 'Anh ấy nói "đi\\về" \U0001f600\nCaf\u00e9 Cafe\u0301',
                        "font_px": 32,
                    }
                ]
            )
        ),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m localization.contracts.fixtures OUT_DIR", file=sys.stderr)
        return 2
    out = Path(argv[0])
    out.mkdir(parents=True, exist_ok=True)
    for name, spec in samples().items():
        (out / f"{name}.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        (out / f"{name}.sha256").write_text(spec_sha256(spec) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Normalises a model-reported box to pixel `[x, y, w, h]`.

The coordinate convention differs per model, so it is configuration
(`OCR_BBOX_FORMAT`), not a guess made at call time.
"""

from collections.abc import Sequence

from localization.config import BBoxFormat
from localization.contracts.geometry import BBox
from localization.providers.errors import PermanentProviderError


def normalize_bbox(raw: Sequence[float], fmt: BBoxFormat, width: int, height: int) -> BBox:
    if len(raw) != 4 or not all(isinstance(v, int | float) for v in raw):
        raise PermanentProviderError("ocr: bbox must be four numbers")
    a, b, c, d = (float(v) for v in raw)
    if fmt == "xyxy_px":
        x1, y1, x2, y2 = a, b, c, d
    elif fmt == "xyxy_norm1":
        x1, y1, x2, y2 = a * width, b * height, c * width, d * height
    elif fmt == "xyxy_norm1000":
        x1, y1, x2, y2 = a * width / 1000, b * height / 1000, c * width / 1000, d * height / 1000
    elif fmt == "yxyx_norm1000":
        y1, x1, y2, x2 = a * height / 1000, b * width / 1000, c * height / 1000, d * width / 1000
    else:  # pragma: no cover - the Literal is closed
        raise PermanentProviderError("ocr: unknown bbox format")
    x1, x2 = sorted((min(max(x1, 0.0), width), min(max(x2, 0.0), width)))
    y1, y2 = sorted((min(max(y1, 0.0), height), min(max(y2, 0.0), height)))
    x, y = round(x1), round(y1)
    w, h = round(x2) - x, round(y2) - y
    if w <= 0 or h <= 0:
        raise PermanentProviderError("ocr: bbox is empty after clamping to the frame")
    return BBox(x=x, y=y, w=w, h=h)

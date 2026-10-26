"""The hash a render is keyed by.

The renderer computes the same hash over the spec it reads, so the two sides must
agree on the bytes that are hashed: JSON with sorted keys, no whitespace and
non-ASCII left as it is. A spec holds only strings and integers, which both
languages write identically.
"""

import hashlib
import json

from localization.contracts.models import EditSpec


def canonical_spec_json(spec: EditSpec) -> str:
    return json.dumps(
        spec.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def spec_sha256(spec: EditSpec) -> str:
    return hashlib.sha256(canonical_spec_json(spec).encode("utf-8")).hexdigest()

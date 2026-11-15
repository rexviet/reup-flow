"""The hash a render is keyed by.

The layout stage hashes the spec it writes and the render hashes the spec it reads
back, so both hash the same canonical bytes: JSON with sorted keys, no whitespace and
non-ASCII left as it is. A spec holds only strings and integers, so the form is the
same whoever writes it. A field added later is left out while it is unset, so a spec
written before it keeps its hash.
"""

import hashlib
import json

from localization.contracts.models import EditSpec


def canonical_spec_json(spec: EditSpec) -> str:
    body = spec.model_dump(mode="json")
    if body["cover"] is not None and not body["cover"]["only_with_captions"]:
        # Preserve the canonical bytes of specs written before timed covers.
        body["cover"].pop("only_with_captions")
    for caption in body["captions"]:
        if caption["caption_color"] is None:
            # Specs written before captions had a colour are drawn white, as before.
            caption.pop("caption_color")
    return json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def spec_sha256(spec: EditSpec) -> str:
    return hashlib.sha256(canonical_spec_json(spec).encode("utf-8")).hexdigest()

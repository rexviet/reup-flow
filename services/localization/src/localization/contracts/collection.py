"""A collection's render settings, as one run sees them.

The starter reads the video's collection when it dispatches the run and captures its
settings here, in the workflow input: editing the collection afterwards never changes
a run already started. The review project copies them into its manifest, so a Studio
save or a revoice, which rebuild the project from the manifest alone, keep them too.

Every type mirrors a CHECK on `public.collections`
(services/downloader/migrations/261114-collections.sql): what the table cannot hold is
refused here as well, so a hand-edited payload cannot carry it either.
"""

from typing import Annotated

from pydantic import Field

from localization.contracts.base import Contract

# Lower case only: the DB stores colours lower case, and one spelling per colour keeps
# the spec hash of a render stable.
HexColor = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]
# A whole percentage, as `Cover.opacity_pct`: no float lands in a spec.
OpacityPct = Annotated[int, Field(ge=0, le=100)]
MAX_MUSIC_OFFSET_DB = 24.0
# Steps of 0.5 dB, exact in binary, as the DB allows.
MusicOffsetDb = Annotated[
    float,
    Field(ge=-MAX_MUSIC_OFFSET_DB, le=MAX_MUSIC_OFFSET_DB, multiple_of=0.5, allow_inf_nan=False),
]
CollectionId = Annotated[
    str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
# Where the cover box goes, as whole percentages of the frame (`activities.cover_geometry`):
# the shift of its centre (of the height, positive is down), its width and its height.
CoverOffsetPct = Annotated[int, Field(ge=-50, le=50)]
CoverWidthPct = Annotated[int, Field(ge=20, le=100)]
CoverHeightPct = Annotated[int, Field(ge=3, le=50)]
# The caption colour of a run whose collection sets none, in both delivery modes.
DEFAULT_CAPTION_COLOR = "#ffff00"


class CollectionSnapshot(Contract):
    """`None` means the collection leaves that setting to the default: the worker's
    configured cover, the yellow caption, the music as the balance sets it."""

    id: CollectionId
    name: Annotated[str, Field(min_length=1, max_length=200)]
    bgm_offset_db: MusicOffsetDb | None = None
    caption_color: HexColor | None = None
    cover_color: HexColor | None = None
    cover_opacity_pct: OpacityPct | None = None
    # `None` keeps the box found from the source subtitles.
    cover_offset_pct: CoverOffsetPct | None = None
    cover_width_pct: CoverWidthPct | None = None
    cover_height_pct: CoverHeightPct | None = None

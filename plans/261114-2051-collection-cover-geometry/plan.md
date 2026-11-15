# Collection cover-box geometry

Status: in progress · Branch: `feat/localization-dubber-code-mode`

## Outcome
A collection can move the subtitle cover box up/down and set its width and height as a share of
the frame. Captions stay centred in the box, so they move with it. The setting applies to new
runs and to videos of the collection still awaiting review (a new review revision is made for
them), with a live preview in the collection dialog. Rendered videos are untouched.

## Decisions (user, 2026-11-14)
- Applies to videos not yet approved, without re-running the workflow.
- The collection dialog shows a preview frame with the box drawn from the values being typed.

## Contract shared by every phase
Columns on `collections` (all nullable; null = today's automatic box):

| column | type | range | meaning |
|---|---|---|---|
| `cover_offset_pct` | smallint | −50..50 | vertical shift of the box centre, % of frame height, positive = down |
| `cover_width_pct` | smallint | 20..100 | box width, % of frame width, centred |
| `cover_height_pct` | smallint | 3..50 | box height, % of frame height |

Geometry, given frame `W×H`, detected band `region` (or none) and the three values:
1. Base box as today. Review path: `top = region.y`, `height = region.h` (fallback `0.8H`, `0.14H`),
   width 60 %. Legacy path: the `Cover.region` after `grow_cover`.
2. `height = round(H * height_pct / 100)` when set, else the base height.
3. `centre = base_top + base_height / 2 + round(H * offset_pct / 100)`; `top = round(centre - height / 2)`.
4. Clamp: `0 ≤ top ≤ H - height`.
5. Width: `width_pct` when set (centred), else the base width. In the review path a long caption may still
   widen its own box, capped at `max(90, width_pct)` %.

Python (`localization`) and TypeScript (`web`) both implement this; each has a test over the same
table of cases, copied into both test files:

| W×H | region (y,h) | offset | width | height | → top, height, width % |
|---|---|---|---|---|---|
| 720×1280 | 1000,120 | – | – | – | 1000, 120, 60 |
| 720×1280 | 1000,120 | 3 | 100 | – | 1038, 120, 100 |
| 720×1280 | 1000,120 | – | – | 12 | 983, 154, 60 |
| 720×1280 | 1000,120 | 50 | – | – | 1160, 120, 60 (clamped) |
| 720×1280 | none | – | 80 | – | 1024, 179, 80 (fallback 0.8H, 0.14H) |

Manifest (`contracts/review_project.py`) and `CollectionSnapshot` gain the same three optional
fields; absent means automatic, so existing manifests and hashes are unchanged (omit nulls from
`spec_hash`).

## Phases
| # | Phase | Owner files | Depends |
|---|---|---|---|
| 1 | Schema + API + form fields | downloader migration/schema/collections, `scripts/apply-migrations.sh`, web types/panel | — |
| 2 | Pipeline geometry | localization contracts, `review_project.py`, `layout_captions.py`, starter `VIDEO_SQL`, `review_bridge/repository.py` | contract |
| 3 | Relayout awaiting videos | `review_worker/` new `cover_relayout.py`, wired into the scan loop | 2 |
| 4 | Preview | downloader endpoint (manifest + signed source link), web preview with TS geometry | 1 |
| 5 | Deploy and check on 7677937301649142961 | — | 1–4 |

### Phase 3 detail
On each review-worker scan, for runs in `needs_human`/`ready_for_review` with a review project,
no `review_decisions` row for that generation and no `final_renders` row: read the live
collection geometry; when it differs from the HEAD manifest's and the local Studio folder is
clean, `prepare` a revision from HEAD (same cues, same visual edits, new geometry,
operation `cover-relayout`) and `activate` it with the HEAD CAS. A dirty folder or a lost CAS
skips the video until the next scan. The localization DB role already has SELECT on collections.

## Acceptance
- Unset values reproduce today's box exactly (existing tests unchanged).
- Setting width 100 % and offset +N moves and widens the box in the review project and the
  final render; captions stay inside it.
- Changing a collection's geometry gives its awaiting videos a new HEAD revision within one scan;
  approved or rendered videos get none; a dirty Studio folder is left alone.
- The dialog preview draws the same box the review project draws for the sample video.
- Migrations up/down round-trip; downloader, localization and web test suites pass (minus the
  6 pre-existing localization workflow failures).

## Non-goals
Per-video geometry overrides; changing OCR band detection; touching already rendered videos.

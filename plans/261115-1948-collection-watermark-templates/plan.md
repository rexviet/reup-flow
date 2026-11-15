# Collection watermark templates

Status: phase 1 done (uncommitted), phases 2–5 not started · Branch: `feat/localization-dubber-code-mode`

## Outcome
A collection holds zero to a few watermark images, each cut by a person as a region on a frame
in the collection dialog. A run of a collection with images finds each image on the source by
template matching and blurs it where and while it shows, in any corner and at the sizes the
spike covered. A collection without images is left as it is: no watermark step, no OCR fallback.
Videos already in review or approved get the blur without re-running the workflow.

## Decisions (user, 2026-11-15)
- Templates per collection, 0..n. None → nothing is done. No OCR fallback.
- The OCR prompt stays as it was. Done: the subtitle + other-text prompt, `detect_text`, the OCR
  watermark grouping and their eval/spike scripts are removed; `vision_ocr.py` matches HEAD.
- The image is picked as a region on a frame in the dashboard.
- Videos awaiting review: applied automatically, like `cover_relayout`.
- Approved videos: keep the approval, move it to the revision with the blur, render the final
  video again (it replaces the old file).
- The collection files carry uncommitted work (collection delete); build on it, do not commit it.

## Evidence (spike, `services/localization/spikes/watermark_template_spike.py`)
17 real videos of two collections + 3 synthetic cases. Method: Canny edges softened with a 7×7
Gaussian, `TM_CCOEFF_NORMED`, threshold 0.7; sizes calibrated per video (0.6–1.5× on 24 frames,
best 3 kept); a hit must hold ≥ 50 % of the template's edge pixels, must not lie ≥ 50 % inside the
subtitle band, and must stay at one place ≥ 2 s (4 frames at 2 fps, gaps ≤ 2 frames bridged).
Results: the handle on 73–99 % of frames in all 6 videos of its collection (several sizes, left
and right corners); the disclaimer on 5 of 10 videos of its collection (top left and top right);
moved / both-corners / 720p synthetic cases 100 %; 0 hits of either template on the other
collection. Cost: 45–170 ms a frame plus ~50 s calibration per video on one CPU thread (~2–3 min
for a 4–5 min video). A visibly different style (a bolder disclaimer, a bottom-right
"剧情纯属虚构" line) needs its own image. Gallery: `data/ocr-bench/watermark-templates/index.html`.

## Contract

### Table `collection_watermarks` (downloader migration) — built
`services/downloader/migrations/261115-collection-watermarks.sql` (+ down).
| column | type | meaning |
|---|---|---|
| `id` | uuid pk | |
| `collection_id` | uuid fk → collections ON DELETE CASCADE | |
| `image_key` | text unique | full object key: `source/collections/{collection_id}/watermarks/{id}.png` |
| `image_sha256` | text | identity of the bytes; what a project records as applied |
| `frame_width`, `frame_height` | int > 0 | size of the frame the region was cut from (base scale) |
| `aweme_id`, `time_ms`, `x`, `y`, `w`, `h` | | where it was cut; region inside the frame, ≥ 8×8 (CHECK) |
| `created_at` | timestamptz | |

Same migration: `GRANT SELECT ON collection_watermarks TO localization` and
`GRANT UPDATE (approved_revision) ON review_decisions TO localization`.

Storage (checked): downloader and workers share `S3_BUCKET` (`reup-media`). The MinIO policy
lets the downloader write only `source/*` and the localization user read `source/*`, so the
images live under the downloader's `S3_KEY_PREFIX` (`source/`) and no policy widens. Only the
downloader's multipart-upload reaper touches `source/`, and it never deletes objects.
Deleting an image or its collection removes the object after the commit (best effort, logged).

### API (downloader, `collections` module) — built
- `GET /api/collections/:id/watermarks` → `{ items: [{ id, image_url, image_sha256, frame_width,
  frame_height, aweme_id, time_ms, x, y, w, h, created_at }] }`.
- `GET /api/collections/:id/watermarks/:watermarkId/image` → the PNG (`image_url` points here).
  Behind the API key like every route, so the dashboard fetches it and shows a blob URL; the
  signed media tokens are tied to a video generation and were not widened for this.
- `POST /api/collections/:id/watermarks?aweme_id&time_ms&frame_width&frame_height&x&y&w&h`
  with the PNG as the body (`Content-Type: image/png`, ≤ 1,000,000 bytes, under nginx's default
  1 MiB). JSON with base64 was dropped: Express's JSON parser stops at 100 KB. The PNG header
  must give exactly `w`×`h`; the video must belong to the collection; at most 10 images (409).
  Needs `STORAGE_BACKEND=s3` (503 otherwise), since the worker reads the image from S3.
- `DELETE /api/collections/:id/watermarks/:watermarkId` → 204.

### Pipeline contracts (localization)
- `WatermarkTemplate { key, sha256, frame_height }`; `CollectionSnapshot.watermarks:
  tuple[WatermarkTemplate, ...] = ()`, captured by the starter like the cover settings.
- Kept as built: `Watermark { region, start_ms, end_ms }`, `SubtitleTrack.watermarks`,
  `SubtitleDocument.watermarks`, `EditSpec.blurs` (hash unchanged when empty), the manifest
  `watermarks`, the ffmpeg blur (`render.py`) and the review HTML blur (`project_html`).
- New manifest field `watermark_templates: tuple[Sha256, ...] = ()`: the template set the
  `watermarks` were found with; the relayout compares it with the collection's current set.
- One module `localization/watermarks/template_match.py`:
  `find(video, templates, subtitle_region) -> tuple[Watermark, ...]`, the spike's method, used by
  both the activity and the relayout. OpenCV (`opencv-python-headless`) added to the
  localization dependencies (worker image and host worker).

## Phases
| # | Phase | Owner files | Depends |
|---|---|---|---|
| 1 | Schema + API — **done** | migration (+ down), `db/schema.ts`, `collections/collection-watermarks.ts`, controller/schemas/module, collection delete cleanup, `scripts/apply-migrations.sh`, `scripts/migration-evidence.sql`, `scripts/tests/apply-migrations.test.sh` | — |
| 2 | Dashboard picker | web `api/client.ts`, `api/types.ts`, new `WatermarkPicker.tsx`, `CollectionsPanel.tsx` | 1 |
| 3 | Matcher + pipeline | `watermarks/template_match.py`, `activities/detect_watermarks.py`, starter `VIDEO_SQL` + snapshot, workflow call behind `workflow.patched`, worker registration, `pyproject.toml` | contract |
| 4 | Existing videos | `review_worker/watermark_relayout.py`, scan loop wiring, `repository.prepare` (watermarks argument) | 3, 1 (grant) |
| 5 | Deploy + check | rebuild web/downloader/workers, host worker deps; check 7680110974204365681 (left and right) and 7704493353806728448 (intermittent) | 1–4 |

### Phase 2 detail
In the collection dialog, a "Watermark" section: the collection's preview video (the existing
`cover-preview` source link), a scrubber to reach a moment where the watermark shows, drag a box
on the frame, "Lưu ảnh mẫu" crops that box from the paused frame on a canvas (the media link is
same-origin, so the canvas is not tainted) and posts it as `image/png` with the region in the
query. Listed images are fetched with the API key and shown as blob URLs. The section lists the images with a
delete button. A collection with no review-project video says why the picker is unavailable.

### Phase 3 detail
`detect_watermarks` runs on the media queue after `detect_subtitles` (it needs the subtitle band)
only when the snapshot has templates; otherwise the workflow skips it. It downloads the source
and the template PNGs (checking each sha256), runs `template_match.find`, writes the result into
the subtitle document, and returns the `SubtitleTrack` with `watermarks` filled. Layout and the
review project then blur them through the paths already built. The workflow change goes behind
`workflow.patched` so runs in flight replay.

### Phase 4 detail
On each review-worker scan, one video per scan at most (detection takes minutes):
1. **Awaiting review** (`ready_for_review`/`needs_human`, no decision, no final render), in a
   collection whose current template set differs from HEAD's `watermark_templates`: detect on
   the project's source snapshot, commit a new revision from HEAD with the new `watermarks` and
   `watermark_templates` (cues and Studio visual edits kept), unless the Studio folder holds
   unsaved edits or HEAD moved (then the next scan retries), as `cover_relayout` does.
2. **Approved**: same detection and revision, based on the approved revision. In one transaction:
   `UPDATE review_decisions SET approved_revision = new WHERE decision = 'approved' AND
   approved_revision = old`, and requeue `final_renders` with `approved_revision = new`, state
   `queued` (never while `rendering`). The final render replaces the old file.
   A folder export already written keeps the old file; exporting again picks up the new one.
3. **Returned** videos are left alone.
Removing every template from a collection makes the next pass commit a revision without blur.

## Tests
- Matcher: the spike's cases as fixtures-free unit tests on generated frames (a template drawn
  at two sizes, left and right, two corners at once, a moving text, text inside the subtitle
  band, a dark frame), plus one real-ffmpeg test.
- Downloader e2e: add / list / delete, size and region limits, cascade on collection delete.
- Web: picker crop and upload (mocked client), list and delete.
- Workflow: the activity runs only with templates; replay of a history recorded before the patch.
- Relayout: awaiting, approved (approval moved, render requeued), returned (untouched), unsaved
  Studio edits (skipped), HEAD moved (retried), templates removed (blur removed).

## Risks
- Matching cost ~2–3 min per video on one thread; the relayout handles one video per scan.
  Optimise later (search the frame borders, a downscaled frame) after measuring in production.
- A style no one cut is not blurred (by decision).
- Re-rendering approved videos replaces delivered files; exported folders keep the old ones.

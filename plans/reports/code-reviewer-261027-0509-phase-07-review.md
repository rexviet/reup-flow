# Phase 07 review: captions layout and renderer

Branch `feat/localization-phase-00-infra`, range `75381fe..HEAD` (09e0bac, a04f571).
Skill `backend-ai-toolkit:code-review` was invoked (spec compliance, then quality, then adversarial).

## Verdict: PASS (no Critical, no High). Seven Medium findings; the phase correctly stays `in-progress`.

## What was run

| Check | Result |
|---|---|
| `uv run pytest -q tests/activities/test_layout_captions.py tests/integration/test_font_parity.py` | 26 passed. Font parity is not skipped (2 passed with `-rs`). |
| `pnpm test` (renderer) | 59 passed |
| `pnpm lint` (renderer) | clean |
| `bash services/renderer/tests/mutations/phase-07.sh` | baseline green; 37 mutations caught, 4 controls stayed green, exit 0; working tree clean afterwards |
| Own probe, real composition | 6 s HEVC 1080x1920 clip rendered through the real asset server and Remotion on the host: ok, no leaked fd |
| Own probe, MinIO with real credentials | renderer HEAD on a missing `work/` key gives 404; on a `source/` key 403 (scope holds) |
| Own probe, Pillow in the worker-media base image | raqm, fribidi, harfbuzz all True |

Not run (so not claimed): `pnpm test:integration` (needs an S3 proxy to the compose MinIO; the author's run is not re-verified), `downloader:infra` harness (edits `docker-compose.yml`), the full compose stack, Python-to-TS trace propagation, a full-length render. The dev MinIO has no `source/` objects, so a real `CopyObject` under the localization policy could not be tried.

## Spec compliance (Stage 1)

Met: layout with PIL at `CAPTION_FONT_PATH`, 2 lines max, 48 down to 32, cue split when it still does not fit, cover grown 6% and clamped, `None` region gives safe zone and no cover, spec stored at `work/{id}/r{round}/edit-spec.json`; renderer key `render-{sha}.mp4`, exists-check, `mkdtemp`, loopback server with random port and per-job token, heartbeat from `onProgress`, `finally` cleanup, queue `render`, `maxConcurrentActivityTaskExecutions`, OTel inbound interceptor; contract fixtures regenerated from Python each run; behavioural font parity at 4 px; overlap test with control; the two required mutations (font config one side, hash-key check) are red.
Deviations are all disclosed in the phase file. Two are documentation-only gaps (see L7). `render_invocations` is a span attribute and an in-process counter, not an exported metric (disclosed).

## Judgement on the cook's decisions

**(a) Source copied to `work/{id}/r0/source.mp4`: ACCEPT.**
- Cost: one server-side copy per video, stored for the 30-day `work/` lifetime. Acceptable.
- Bytes: `ObjectStore.copy` uses `copy_object`; the payload carries keys only, so principle 2 holds. The renderer's key stays `work/*` only (confirmed: 403 on `source/`).
- Idempotency: the copy is skipped when the key exists, and the copy itself is atomic. Weakness: the existence check is by key, not by content (L3).
- Naming: `r0` matches the other round-0 artifacts (`vocals`, `bgm`, `subtitles.json`). Fine.
- Unverified: `CopyObject` under the real localization policy (L4).

**(b) EditSpec gained `cover.color`, `cover.opacity_pct`, `caption_box`, `Caption.font_px`: ACCEPT.**
- Parity: `contracts/edit-spec.schema.json` is regenerated and drift-tested; Node validates 3 Python-generated fixtures per run with `strict` Ajv; hash parity is tested (NFD, emoji, quotes) and the sort-order mutation is red on both sides. Integer `opacity_pct` correctly avoids float formatting differences.
- Hash stability: no `EditSpec` was ever produced by an activity before this commit (only fixtures), so no stored history or cache is invalidated. Adding any field later changes every hash and there is no `spec_version` (L8).
- Secrets and URLs: the schema is closed (`additionalProperties: false`), there is no URL field, `color` is `^#[0-9a-f]{6}$`, keys are re-checked to be under `work/{aweme_id}/` at runtime, caption text is rendered as escaped React text. The renderer cannot be handed a secret or a URL through the spec.
- `COVER_*` read by worker-media only is the better design; plan §6 should be updated to match (L7).

**(c) Renderer tests in `tests/`: ACCEPT.** The runner's `harness_dir` expects `tests/mutations`. Update the phase file's file list.

## Findings

### Critical / High
None.

### Medium

**M1. Untrusted ffprobe values reach the renderer unbounded.**
`services/localization/src/localization/activities/ffmpeg.py:48-86` takes width, height, fps, duration from the downloaded Douyin file. `contracts/models.py` (`EditSpec`: `PositiveInt` only, no maximum; `captions` has no `maxItems`) and `contracts/edit-spec.schema.json` impose no ceiling. `services/renderer/src/remotion/Root.tsx` and `src/frames.ts` turn them straight into the composition size and frame count.
Scenario: a crafted file reports fps 1e6 or duration 1e7 s; the renderer allocates or renders for hours while `onProgress` heartbeats keep it alive, holding the single render slot.
Fix: cap in the Pydantic model and regenerate the schema (for example width and height at most 4096, fps at most 120, duration at most the product limit, captions at most a few thousand); reject as non-retryable.

**M2. Font identity is only the file name.**
`services/renderer/src/activities/render.ts:85-88` compares `basename(fontPath)` with `spec.font`. worker-media bind-mounts `./assets/fonts` live (`docker-compose.yml`, worker-media `volumes`), while the renderer image bakes a copy at build (`services/renderer/Dockerfile`, `COPY assets/fonts`). `compose-constraints.spec.ts` only checks that the two env strings are equal.
Scenario: the font file is replaced and only worker-media restarts; names match, bytes differ, the layout measures one font and Chromium draws another. The font parity test would not run in production.
Fix: put the font's sha256 in the spec (layout computes it, renderer verifies it and fails non-retryably), or mount the same directory into both services.

**M3. Glyphs missing from the font are not detected.**
`layout_captions.py` (`FontMeter`) never checks coverage. Verified: with `BeVietnamPro-SemiBold.ttf`, `你` and `😀` render as .notdef (PIL advance 0.5 em). A Vietnamese line that keeps a Chinese name or an emoji is measured with .notdef widths, while the slim Chromium image has no fallback font, so tofu boxes are burned into the video.
Fix: in `lay_out`, check each character against the font cmap (or the .notdef mask trick); raise a non-retryable error naming the segment so the workflow can route it to the localizer or `needs_human`.

**M4. Same-spec requests are not de-duplicated while in flight, and the activity ignores cancellation.**
`render.ts:97-135`: the exists-check happens before the render, nothing marks a render as running. With `RENDER_CONCURRENCY` above 1, or two `worker-render` replicas, two requests for the same spec both call `renderMedia` and both upload (same key, last write wins, no corruption; wasted CPU). With the default of 1 on one worker they serialize and the second returns the key, so the default is safe. Also `renderMedia` is not given a `cancelSignal`, so a cancelled or timed-out activity keeps the slot until the render ends, and nothing heartbeats during the source download or the upload, which phase 08's heartbeat timeout may kill.
Fix: a per-process `Map<sha, Promise>` single-flight (cheap), pass `Context.current().cancellationSignal` to `renderMedia`, heartbeat around download and upload. Add a test that fires the same spec twice with limit 3 and asserts one `renderMedia`.

**M5. Memory budget is inconsistent and unmeasured.**
`docker-compose.yml` worker-render: `mem_limit: 4g`, `tmpfs /tmp:size=4g`, `shm_size: 1gb`. tmpfs and shm pages count against the cgroup, and Remotion writes frames, the bundle and the output under `/tmp` while Chromium needs RAM. A 60 s 1080x1920 render has not been done (disclosed), so the real peak is unknown; the ceilings overlap enough that the OOM killer can win before the tmpfs cap does.
Fix: render a 60 s clip in the container, record peak RSS plus `/tmp` use, then set `mem_limit` above `tmpfs + shm + Chromium` or shrink the tmpfs.

**M6. Cover and caption box trust the detected region without a sanity bound.**
`layout_captions.py` `lay_out` / `grow_cover`: bbox convention is handled safely upstream (`providers/bbox.py` normalises by the required `OCR_BBOX_FORMAT`, clamps, rejects empty boxes, and `detect_subtitles.to_source` rescales to source pixels), so the layout receives pixel boxes inside the frame. But G5 has not run, and the region is a union over frames in a band: one stray detection can make the cover a large part of the frame at 92% opacity, and `caption_box` equals the cover, so a thin cover makes two-line text overflow (known limit, disclosed).
Fix: reject or fall back to the safe zone when the cover exceeds a fraction of the frame (for example more than 35% of the height), and enforce a minimum caption box height of two lines at the chosen size.

**M7. Missing-object detection relies on HEAD returning 404 under a prefix-conditioned ListBucket policy.**
`services/renderer/src/object-store.ts:76-83` and the `renderer` policy in `infra/minio/init.sh`. Verified on MinIO: 404 for a missing `work/` key. On AWS S3 and possibly R2 (the plan's production stores) a principal without an unconditioned `s3:ListBucket` gets 403 for a missing key; the existence check would then throw a retryable error forever and no render would ever start. The localization policy has the same shape (`ObjectStore.exists`).
Fix: verify against the production store before phase 10; if 403 occurs, treat 403 on that key as missing (the policy already confines the key space) or grant unconditioned ListBucket scoped by resource.

### Low

- **L1. Asset server leaks a file descriptor per aborted request.** `src/asset-server.ts` uses `fs.createReadStream(...).pipe(res)` without error handling or destroy on client abort. Verified with a probe: 20 aborted range requests left 20 open fds that survive `server.close()` and `rmSync`, holding the deleted file's blocks on tmpfs. Real Remotion 4.0.535 on a 6 s clip did not trigger it, so it is latent. Fix: `stream.pipeline(read, res, () => {})`.
- **L2. Malformed spec JSON is retryable.** `render.ts` `JSON.parse` runs before validation; a `SyntaxError` is retried under the default policy. Fix: wrap in `InvalidEditSpec` non-retryable.
- **L3. `r0/source.mp4` is skipped when it exists, and the spec carries no source hash**, so the render key does not cover the picture bytes. A re-downloaded different source for the same id would be rendered stale. Fix: always copy (cheap), or add the source ETag or sha256 to the spec.
- **L4. `CopyObject` is only tested under moto, which does not enforce IAM.** No test copies `source/` to `work/` with the real localization policy. Do this in the phase 08 stack run.
- **L5. raqm is not asserted.** `FontMeter` uses PIL's default engine. raqm is available in the worker-media base image today (verified, fribidi arrives with ffmpeg), but a missing libfribidi would silently fall back to basic layout and drift from Chromium. Fix: assert `features.check("raqm")` at worker start.
- **L6. No `.dockerignore` with a repo-root build context.** `.env`, `data/`, `backups/` and `node_modules` go to the daemon (not baked: the Dockerfile copies explicit paths). Apt packages are unpinned and `curl`/`unzip` stay in the runtime image.
- **L7. Docs.** `plan.md` §6 still lists `COVER_COLOR`/`COVER_OPACITY` for worker-render, and the phase file's "Files" list still says `test/`; deviations (b) and (c) are recorded only under "Khac voi phase file".
- **L8. No spec version and no deploy-skew story.** A new Python field with an old renderer image gives a permanent `InvalidEditSpec`. Acceptable, but add `spec_version` before the first field change after launch.
- **L9. Rotation and VFR are untested.** Ingest uses coded width and height from ffprobe; a video with rotate metadata would be drawn with the wrong box. HEVC works (my probe and the integration test); other codecs are untested and an unsupported codec is a retryable Remotion error.
- **L10. Caption edge cases.** A segment with the same `start_ms` as the next one gets no caption silently (`caption_spans`, `end > start` guard). A very short span split into many pieces yields sub-frame cues (40 ms over 9 pieces is 4 ms each), which Remotion draws as overlapping 1-frame cues. Add a minimum cue length or a `needs_human` signal.

## Focus areas, short answers

- **Key by hash / idempotency:** correct for sequential calls and retries (upload is atomic; retry after a post-upload crash returns the key). In-flight dedupe: M4.
- **HTTP localhost serving:** binds 127.0.0.1, port 0, 24-byte random token, basename plus snapshot-of-listing guard (traversal blocked, tested), 404 without token, closed in `finally`, no compose port, token never leaves the container except in Remotion's own error text. Only L1.
- **Docker image:** base pinned by digest, Chrome pinned by sha256 for both arches and the build fails on mismatch, `--frozen-lockfile`, non-root `node`, no secrets baked, no published ports (stricter than "127.0.0.1"), env allowlist enforced by `compose-constraints.spec.ts`, `shm_size` set (M5 for sizing).
- **Credential scope:** renderer user is `work/*` only (verified 403 on `source/`); the localization key is not in worker-render (tested and mutation-covered).
- **Caption layout:** PIL at 32x scale with raqm, max 2 lines, 48 to 32, balanced split, NFC and NFD measure identically, empty text gives no caption, one over-long word is placed alone at 32 px (disclosed), RTL not needed; M3, M6, L10 above. Font parity 4 px test is behavioural.
- **HEVC:** works through the compositor; verified.
- **RENDER_CONCURRENCY:** enforced in `createProductionWorker` (the one place a Worker is built), env bounded 1 to 64, default 1, control shows the probe can see overlap. Compose carries it; replicas multiply it (documented nowhere, see M4).
- **OTel:** inbound interceptor present and tested with a TS client; Python-to-TS propagation not re-verified (honestly stated). No secrets in span attributes (only the spec hash).
- **Error classification:** InvalidInput, InvalidEditSpec, FontMismatch, AudioHashMismatch, ArtifactMissing are non-retryable; Chromium and S3 errors are retryable. Gaps: L2, L9.
- **Mutation tests:** load-bearing. Each mutation names the tests that must fail and the protocol matches the failing test names; the contract mutation compares against Python-generated hashes (a real cross-language oracle); the overlap test has a control; font tests measure rendered pixels. No test compared mutated code with itself. Missing mutation coverage: same-spec concurrency (nothing to cover yet, M4), cover geometry inside Chromium (integration only, needs MinIO).
- **Fixtures/doubles:** the memory store raises the same non-retryable `ArtifactMissing` as the real store, and the real-store path is covered by the MinIO integration suite; moto cannot model IAM (L4).
- **Phase status honesty:** accurate. The "not verified" list in the phase file matches what I found; nothing claimed that I could not reproduce.

## Cleanup
Temporary build output in `services/renderer/probe_out` removed; my socat proxy container removed; no background processes left; the Temporal dev server of another session was untouched. Probe scripts are in the session scratchpad only.

## Unresolved questions
- Which production object store (AWS S3 or R2)? It decides whether M7 is real.
- Is an upper bound on source duration part of the product contract (M1)?

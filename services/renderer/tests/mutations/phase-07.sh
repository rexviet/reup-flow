#!/usr/bin/env bash
#
# Mutation suite for captions and the renderer:
#
#   - a spec is rendered once: the output is keyed by the   (tests/render.spec.ts)
#     hash of the spec, an existing key is returned, the
#     audio and font are the ones the spec names
#   - the render's folder and server never outlive it       (tests/render.spec.ts)
#   - the asset server: token, loopback, no path escape      (tests/asset-server.spec.ts)
#   - the contract: the schema is enforced, the hash is the  (tests/edit-spec.contract.spec.ts)
#     one Python computes
#   - one render at a time, and the trace carried into the   (tests/*.integration-spec.ts)
#     worker
#   - the layout: cover, line breaking, sizes, cue timing,   (localization tests)
#     one translation, the spec stored with what it was
#     made for
#   - the font that is measured is the font that is drawn    (localization test_font_parity)
#
# A guard is evidence only if breaking its invariant turns it red, and the controls say
# WHICH mechanism carries it. Protocol and rules: scripts/lib/mutation-protocol.sh. The
# jest suites report through tests/mutations/jest-protocol.js, which prints the failing
# tests the way pytest does.
#
# Run from anywhere:  bash services/renderer/tests/mutations/phase-07.sh
# Needs `pnpm install`, `uv sync` in services/localization, and ffmpeg. The two
# integration suites start a local Temporal dev server; the font parity test renders in
# Chromium.

set -u

cd "$(dirname "$0")/../.." || exit 1
# shellcheck source=../../../../scripts/lib/mutation-protocol.sh
source ../../scripts/lib/mutation-protocol.sh

RENDER=src/activities/render.ts
ASSETS=src/asset-server.ts
SPECTS=src/edit-spec.ts
CONFIG=src/config.ts
WORKER=src/worker.ts
TRACING=src/tracing.ts
VIDEO=src/remotion/LocalizedVideo.tsx
FONT=src/font.ts
TSSTORE=src/object-store.ts
L=../localization/src/localization
LAYOUT=$L/activities/layout_captions.py
SPECHASH=$L/contracts/spec_hash.py
MODELS=$L/contracts/models.py
PYSTORE=$L/storage/object_store.py

mp_init "$RENDER" "$ASSETS" "$SPECTS" "$CONFIG" "$WORKER" "$TRACING" "$VIDEO" \
  "$LAYOUT" "$SPECHASH" "$MODELS" "$FONT" "$TSSTORE" "$PYSTORE"
MP_TIMEOUT_SECONDS=${MP_TIMEOUT_SECONDS:-300}

JEST=(node tests/mutations/jest-protocol.js)
# Python tests live in the other service: run pytest there, with paths relative to it.
PYTEST=(bash -c 'cd ../localization && exec uv run pytest -q -p no:cacheprovider "$@"' _)

T_RENDER=tests/render.spec.ts
T_ASSETS=tests/asset-server.spec.ts
T_CONTRACT=tests/edit-spec.contract.spec.ts
T_CONFIG=tests/config.spec.ts
T_STORE=tests/object-store.spec.ts
T_CONC=tests/concurrency.integration-spec.ts
T_TRACE=tests/tracing.integration-spec.ts
P_LAYOUT=tests/activities/test_layout_captions.py
P_PARITY=tests/integration/test_font_parity.py
P_ARCH=tests/architecture/test_single_translation.py
P_DRIFT=tests/test_edit_spec_schema_drift.py
P_STORE=tests/test_object_store.py

MP_TEST_CMD=("${JEST[@]}")
mp_baseline "$T_RENDER $T_ASSETS $T_CONTRACT $T_CONFIG $T_STORE"
mp_baseline "$T_CONC $T_TRACE"
MP_TEST_CMD=("${PYTEST[@]}")
mp_baseline "$P_LAYOUT $P_PARITY $P_ARCH $P_DRIFT $P_STORE"

use_jest() { MP_TEST_CMD=("${JEST[@]}"); }
use_pytest() { MP_TEST_CMD=("${PYTEST[@]}"); }

# --- a spec is rendered once ----------------------------------------------------------------------------
use_jest
mp_mutation "an existing render is never looked for"
apply_mutation "$RENDER" 'if (await deps.store.exists(outputKey)) {' 'if (false) {' \
  && expect_red "$T_RENDER" \
    "does not render a spec that is already rendered" \
    "finds an earlier render by its key alone"
restore

mp_mutation "the output key does not carry the hash of the spec"
apply_mutation "$RENDER" 'return `work/${awemeId}/render-${specSha}.mp4`;' 'return `work/${awemeId}/render.mp4`;' \
  && expect_red "$T_RENDER" \
    "is the hash of the spec" \
    "renders again, under another key, when a caption changed"
restore

mp_mutation "the audio is not checked against the hash in the spec"
apply_mutation "$RENDER" 'if (sha256File(audio) !== spec.audio_sha256) {' 'if (false) {' \
  && expect_red "$T_RENDER" "a track that is not the one the spec was made for"
restore

mp_mutation "the font is not checked against the one the spec was laid out with"
apply_mutation "$FONT" 'if (name !== spec.font) {' 'if (false) {' \
  && expect_red "$T_RENDER" "a spec laid out with another font"
restore

mp_mutation "a spec may name another video's files"
apply_mutation "$RENDER" 'for (const key of [spec.source_key, spec.audio_key]) {' 'for (const key of [] as string[]) {' \
  && expect_red "$T_RENDER" "a spec that points at another video"
restore

# --- nothing outlives a render ---------------------------------------------------------------------------
mp_mutation "the render's folder is not removed"
apply_mutation "$RENDER" '      if (dir) fs.rmSync(dir, { recursive: true, force: true });' '' \
  && expect_red "$T_RENDER" \
    "removes its folder and stops its server when it succeeds" \
    "removes its folder, stops its server and uploads nothing when the renderer fails"
restore

mp_mutation "the render's server is not stopped"
apply_mutation "$RENDER" '      await server?.close();' '' \
  && expect_red "$T_RENDER" \
    "removes its folder and stops its server when it succeeds" \
    "removes its folder, stops its server and uploads nothing when the renderer fails"
restore

# --- the asset server -------------------------------------------------------------------------------------
mp_mutation "the server's token is the same every time"
apply_mutation "$ASSETS" 'const token = randomBytes(24).toString("hex");' 'const token = "fixed";' \
  && expect_red "$T_ASSETS" "listens on a different random port"
restore

mp_mutation "the server listens on every interface"
apply_mutation "$ASSETS" 'server.listen(0, "127.0.0.1", () => resolve());' 'server.listen(0, "0.0.0.0", () => resolve());' \
  && expect_red "$T_ASSETS" "is reachable on loopback and on no other interface"
restore

mp_mutation "the server serves any path under its directory, not only the job's files"
apply_mutation "$ASSETS" 'if (name === "" || name !== path.basename(name) || !files.has(name)) {' 'if (name === "") {' \
  && expect_red "$T_ASSETS" "does not hand out a file that is not in the directory"
restore

mp_mutation "the server drops its CORS header"
apply_mutation "$ASSETS" '    res.setHeader("Access-Control-Allow-Origin", "*");' '' \
  && expect_red "$T_ASSETS" "allows the composition page"
restore

mp_mutation "the server answers without the job's token"
apply_mutation "$ASSETS" '    if (url.pathname.startsWith(prefix)) {' '    if (true) {' \
  "$ASSETS" '        name = decodeURIComponent(url.pathname.slice(prefix.length));' '        name = decodeURIComponent(url.pathname.split("/").pop() ?? "");' \
  && expect_red "$T_ASSETS" "answers 404 without the token"
restore

# --- the contract -----------------------------------------------------------------------------------------
mp_mutation "the renderer accepts any spec"
apply_mutation "$SPECTS" '    if (!validate(value)) {' '    if (false) {' \
  && expect_red "$T_CONTRACT" "an unknown field" "a missing field"
restore

mp_mutation "the renderer hashes keys in the order they were written"
apply_mutation "$SPECTS" $'    const members = Object.keys(record)\n      .sort()\n' $'    const members = Object.keys(record)\n' \
  && expect_red "$T_CONTRACT" "with-cover hashes to what Python says" "does not depend on the order keys"
restore

mp_mutation "Python hashes keys in the order they were written"
apply_mutation "$SPECHASH" 'sort_keys=True' 'sort_keys=False' \
  && expect_red "$T_CONTRACT" "with-cover hashes to what Python says"
restore

use_pytest
mp_mutation "the model gains a field and the schema is not regenerated"
apply_mutation "$MODELS" $'    font_px: PositiveInt\n' $'    font_px: PositiveInt\n    shadow_px: PositiveInt = 2\n' \
  && expect_red "$P_DRIFT" "test_stored_schema_matches_the_model"
restore

# --- one render at a time; the trace reaches the worker ----------------------------------------------------
use_jest
mp_mutation "the concurrency limit is not applied to the worker"
apply_mutation "$WORKER" '    maxConcurrentActivityTaskExecutions: settings.renderConcurrency,' '' \
  && expect_red "$T_CONC" "runs one render at a time"
restore

mp_mutation "the default render concurrency is eight"
apply_mutation "$CONFIG" 'if (raw === undefined || raw === "") return 1;' 'if (raw === undefined || raw === "") return 8;' \
  && expect_red "$T_CONFIG" "render concurrency is one unless it is set"
restore

mp_mutation "the worker has no tracing interceptor"
apply_mutation "$TRACING" '    activity: [(ctx) => ({ inbound: new OpenTelemetryActivityInboundInterceptor(ctx) })],' '    activity: [],' \
  && expect_red "$T_TRACE" "runs the render activity inside the trace"
restore

# --- the font that is measured is the font that is drawn ---------------------------------------------------
use_pytest
mp_mutation "the layout measures with another font than the one configured"
apply_mutation "$LAYOUT" \
  'font = self._fonts[size] = ImageFont.truetype(self.path, size * MEASURE_SCALE)' \
  'font = self._fonts[size] = ImageFont.load_default(size * MEASURE_SCALE)' \
  "$LAYOUT" \
  'font = self._inked[size] = ImageFont.truetype(self.path, size * INK_SCALE)' \
  'font = self._inked[size] = ImageFont.load_default(size * INK_SCALE)' \
  && expect_red "$P_PARITY" "test_the_text_is_drawn_as_wide_as_the_layout_measured_it"
restore

mp_mutation "the renderer draws with another font than the one it was given"
apply_mutation "$VIDEO" '      fontFamily: FONT_FAMILY,' '      fontFamily: "serif",' \
  && expect_red "$P_PARITY" "test_the_text_is_drawn_as_wide_as_the_layout_measured_it"
restore

# --- the layout ---------------------------------------------------------------------------------------------
mp_mutation "the cover is not grown"
apply_mutation "$LAYOUT" '    margin = round(COVER_MARGIN_FRACTION * frame_w)' '    margin = 0' \
  && expect_red "$P_LAYOUT" "test_the_cover_is_the_text_box_plus_a_thin_margin_on_each_side"
restore

mp_mutation "a narrow burned-in line gets a cover as narrow as the line"
apply_mutation "$LAYOUT" '    if right - left < minimum:' '    if False:' \
  && expect_red "$P_LAYOUT" "test_a_narrow_burned_in_line_gets_a_cover_wide_enough_for_a_longer_caption"
restore

mp_mutation "the drawn cover is shorter than the box the captions are laid out in"
apply_mutation "$LAYOUT" \
  '        cover = Cover(region=box, color=cover_color, opacity_pct=round(cover_opacity * 100))' \
  '        cover = Cover(region=subtitles.region, color=cover_color, opacity_pct=round(cover_opacity * 100))' \
  && expect_red "$P_LAYOUT" \
    "test_the_drawn_cover_is_the_box_the_captions_are_laid_out_in" \
    "test_a_thin_cover_still_gets_a_caption_box_of_two_lines"
restore

mp_mutation "the grown cover may leave the frame"
apply_mutation "$LAYOUT" 'left, top = max(0, region.x - margin), max(0, region.y - margin)' 'left, top = region.x - margin, region.y - margin' \
  && expect_red "$P_LAYOUT" "test_the_cover_never_leaves_the_frame"
restore

mp_mutation "a line may be as wide as it likes"
apply_mutation "$LAYOUT" 'if widest <= max_width and (best is None or widest < best[0]):' 'if best is None or widest < best[0]:' \
  && expect_red "$P_LAYOUT" "test_a_longer_line_takes_two_lines_and_the_largest_size_that_fits"
restore

mp_mutation "there is only one font size"
apply_mutation "$LAYOUT" 'FONT_SIZES = (48, 44, 40, 36, 32)' 'FONT_SIZES = (48,)' \
  && expect_red "$P_LAYOUT" "test_a_longer_line_takes_two_lines_and_the_largest_size_that_fits"
restore

mp_mutation "a line that does not fit is never split into cues"
apply_mutation "$LAYOUT" 'pieces = [whole] if whole else split_to_fit(text, meter, max_width)' 'pieces = [whole or Fitted((text,), FONT_SIZES[-1])]' \
  && expect_red "$P_LAYOUT" "test_every_line_of_every_caption_fits_the_box_at_its_own_size"
restore

mp_mutation "a caption runs into the next one"
apply_mutation "$LAYOUT" '            end = min(end, ordered[index + 1].start_ms)' '            end = end' \
  && expect_red "$P_LAYOUT" "test_a_caption_is_shown_while_its_line_is_spoken_and_never_into_the_next"
restore

mp_mutation "a caption outlasts the video"
apply_mutation "$LAYOUT" '        end = min(end, duration_ms)' '        end = end' \
  && expect_red "$P_LAYOUT" "test_a_caption_never_outlasts_the_video"
restore

mp_mutation "a track made from another script version is accepted"
apply_mutation "$LAYOUT" 'if dub.script_version != ref.version or dub.script_key != ref.script_key:' 'if False:' \
  "$LAYOUT" 'if doc.script_version != ref.version or script.version != ref.version:' 'if False:' \
  && expect_red "$P_LAYOUT" "test_a_track_made_from_another_script_version_is_refused_for_good"
restore

mp_mutation "a line spoken differently from how it is written is accepted"
apply_mutation "$LAYOUT" 'if spoken.get(segment.id) != segment.text_vi:' 'if False:' \
  && expect_red "$P_LAYOUT" "test_a_line_spoken_differently_from_how_it_is_written_is_refused_for_good"
restore

mp_mutation "the returned hash is not the hash of the stored spec"
apply_mutation "$LAYOUT" 'spec_sha256=spec_sha256(edit_spec),' 'spec_sha256="0" * 64,' \
  && expect_red "$P_LAYOUT" "test_the_returned_hash_is_the_hash_of_the_stored_spec"
restore

mp_mutation "the spec is stored under round zero whatever the round"
apply_mutation "$LAYOUT" 'spec_key = work_key(spec.aweme_id, round, "edit-spec.json")' 'spec_key = work_key(spec.aweme_id, 0, "edit-spec.json")' \
  && expect_red "$P_LAYOUT" "test_the_spec_is_stored_under_the_round_and_carries_what_it_was_made_for"
restore

mp_mutation "the spec does not carry the script version"
apply_mutation "$LAYOUT" '        script_version=script.version,' '        script_version=0,' \
  && expect_red "$P_LAYOUT" "test_the_spec_is_stored_under_the_round_and_carries_what_it_was_made_for"
restore

mp_mutation "the spec does not carry the audio hash"
apply_mutation "$LAYOUT" '        audio_sha256=dub.audio_sha256,' '        audio_sha256="c" * 64,' \
  && expect_red "$P_LAYOUT" "test_the_spec_is_stored_under_the_round_and_carries_what_it_was_made_for"
restore

mp_mutation "the renderer is pointed at the source folder, which it cannot read"
apply_mutation "$LAYOUT" 'source_key = work_key(spec.aweme_id, 0, "source.mp4")' 'source_key = spec.video_key' \
  && expect_red "$P_LAYOUT" "test_the_source_video_is_copied_into_work_for_the_renderer"
restore

mp_mutation "a video with no burned-in subtitle gets a cover anyway"
apply_mutation "$LAYOUT" 'if subtitles.region is not None:' 'if True:' \
  && expect_red "$P_LAYOUT" "test_without_a_burned_in_subtitle_there_is_no_cover"
restore

mp_mutation "the layout stage reaches an agent"
apply_mutation "$LAYOUT" '@append' $'\nimport localization.agents.localizer  # noqa: F401\n' \
  && expect_red "$P_ARCH" "test_stage_does_not_reach_an_agent"
restore

# --- a render is shared, bounded in time and stoppable ---------------------------------------------------
use_jest
mp_mutation "two requests for the same spec each start a render"
apply_mutation "$RENDER" '    let shared = inFlight.get(sha);' '    let shared: SharedRender | undefined;' \
  && expect_red "$T_RENDER" "renders it once and gives both the same answer"
restore

mp_mutation "a render has no deadline"
apply_mutation "$RENDER" $'    const timeout = AbortSignal.timeout(\n      deps.deadlineMs ?? renderDeadlineMs(spec.duration_ms),\n    );' '    const timeout = new AbortController().signal;' \
  && expect_red "$T_RENDER" "gives up for good on a render that outlives its deadline"
restore

mp_mutation "the first caller's cancellation stops a render that others still wait for"
apply_mutation "$RENDER" '      if (joined.callers === 0) {' '      if (true) {' \
  && expect_red "$T_RENDER" "keeps rendering for the one that stays" "keeps rendering for the first when only the one that joined goes"
restore

mp_mutation "a caller that is cancelled keeps waiting for the render"
apply_mutation "$RENDER" 'return await untilCancelled(joined.promise, own);' 'return await joined.promise;' \
  && expect_red "$T_RENDER" "a caller that is cancelled stops waiting even though the render goes on"
restore

mp_mutation "the render goes on after its last caller has gone"
apply_mutation "$RENDER" $'        joined.stop.abort(new Error("every caller of this render has gone"));\n' '' \
  && expect_red "$T_RENDER" "stops the render, and leaves nothing, when the last one has gone"
restore

mp_mutation "a caller joins a render that is being stopped"
apply_mutation "$RENDER" $'        if (inFlight.get(sha) === joined) inFlight.delete(sha);\n        joined.stop.abort' $'        joined.stop.abort' \
  && expect_red "$T_RENDER" "a caller that comes while the last one is stopping the render starts its own"
restore

mp_mutation "a deadline that falls while downloading is retried"
apply_mutation "$RENDER" 'if (timeout.aborted && !shared.stop.signal.aborted) {' 'if (false) {' \
  && expect_red "$T_RENDER" "ends a render with the deadline, for good, even when the time ran out while downloading" "gives up for good on a render that outlives its deadline"
restore

mp_mutation "the deadline does not grow with the video"
apply_mutation "$RENDER" 'RENDER_DEADLINE_PER_VIDEO_MS = 6;' 'RENDER_DEADLINE_PER_VIDEO_MS = 0;' \
  && expect_red "$T_RENDER" "grows with the length of the video" "gives the longest video the spec allows more than"
restore

mp_mutation "the renderer is not given the signal that stops it"
apply_mutation "$RENDER" $'          signal,\n        });' $'        });' \
  && expect_red "$T_RENDER" "passes the activity" "gives up for good on a render that outlives"
restore

mp_mutation "nothing heartbeats between the renderer's reports"
apply_mutation "$RENDER" 'deps.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS' '1e9' \
  && expect_red "$T_RENDER" "heartbeats while it downloads"
restore

mp_mutation "the heartbeat timer outlives the render"
apply_mutation "$RENDER" '      clearInterval(beat);' '' \
  && expect_red "$T_RENDER" "stops heartbeating when the render is over"
restore

mp_mutation "a spec that is not JSON is retried"
apply_mutation "$RENDER" 'refuse("the edit spec is not valid JSON", "InvalidEditSpec");' 'throw new Error("bad json");' \
  && expect_red "$T_RENDER" "a spec that is not JSON"
restore

# --- the font is identified by its bytes -----------------------------------------------------------------
mp_mutation "the renderer does not compare the font's hash"
apply_mutation "$FONT" 'if (fontSha256(fontPath) !== spec.font_sha256) {' 'if (false) {' \
  && expect_red "$T_RENDER" "a font with the right name and other bytes"
restore

# --- a missing object may be told as 403 -----------------------------------------------------------------
mp_mutation "the renderer treats a 403 on HEAD as a failure"
apply_mutation "$TSSTORE" 'if (isMissing(err) || (err as AwsError).$metadata?.httpStatusCode === 403) return false;' 'if (isMissing(err)) return false;' \
  && expect_red "$T_STORE" "says an object is missing when the store answers 403"
restore

# --- the asset server closes what it opens ---------------------------------------------------------------
mp_mutation "a client that hangs up mid-file leaves the file open"
apply_mutation "$ASSETS" 'pipeline(fs.createReadStream(file, { start, end }), res, () => undefined);' 'fs.createReadStream(file, { start, end }).pipe(res);' \
  && expect_red "$T_ASSETS" "closes the file when a client goes away"
restore

use_pytest
mp_mutation "the Python store treats a 403 on HEAD as a failure"
apply_mutation "$PYSTORE" 'return _is_missing(exc) or code in ("403", "Forbidden", "AccessDenied") or status == 403' 'return _is_missing(exc)' \
  && expect_red "$P_STORE" "test_a_403_on_head_means_the_object_is_not_there"
restore

mp_mutation "the layout writes a hash that is not the font's"
apply_mutation "$LAYOUT" '        font_sha256=meter.sha256,' '        font_sha256="0" * 64,' \
  && expect_red "$P_LAYOUT" "test_the_spec_names_the_font_by_the_hash_of_its_bytes"
restore

mp_mutation "the layout does not look for characters the font lacks"
apply_mutation "$LAYOUT" $'    check_glyph_coverage(spans, meter)\n' '' \
  && expect_red "$P_LAYOUT" "test_a_character_the_font_lacks_is_refused_for_good_naming_the_segment"
restore

mp_mutation "the layout does not check the video against the renderer's limits"
apply_mutation "$LAYOUT" $'        check_renderable(ingest)\n' '' \
  && expect_red "$P_LAYOUT" "test_a_video_too_large_for_the_renderer_is_refused_for_good"
restore

mp_mutation "the spec model allows frames of any size"
apply_mutation "$MODELS" 'MAX_FRAME_PX = 4096' 'MAX_FRAME_PX = 100000' \
  && expect_red "$P_LAYOUT" "test_the_spec_model_enforces_the_same_limits" \
  && true
restore

mp_mutation "a stray detection becomes a cover over most of the frame"
apply_mutation "$LAYOUT" $'        check_cover_plausible(subtitles.region, ingest.width, ingest.height)\n' '' \
  && expect_red "$P_LAYOUT" "test_a_detected_region_too_large_to_be_a_subtitle_is_refused"
restore

mp_mutation "a thin cover leaves a caption box too small for two lines"
apply_mutation "$LAYOUT" '        caption_box_for(cover.region, ingest.height)' '        cover.region' \
  && expect_red "$P_LAYOUT" "test_a_thin_cover_still_gets_a_caption_box_of_two_lines" "test_the_cover_is_grown_and_the_captions_sit_in_it"
restore

mp_mutation "a source downloaded again is not copied again"
apply_mutation "$LAYOUT" $'        self._store.copy(spec.video_key, source_key, produced_by="ingest")\n' $'        if not self._store.exists(source_key):\n            self._store.copy(spec.video_key, source_key, produced_by="ingest")\n' \
  && expect_red "$P_LAYOUT" "test_a_source_downloaded_again_is_copied_again"
restore

mp_mutation "the layout stage starts without raqm"
apply_mutation "$LAYOUT" 'if not features.check("raqm"):' 'if False:' \
  && expect_red "$P_LAYOUT" "test_the_layout_stage_refuses_to_start_without_raqm"
restore

# --- controls: changes that are not invariants ---------------------------------------------------------------
use_jest
mp_mutation "control: the server's directory check alone is removed (its file list still holds)"
apply_mutation "$ASSETS" 'if (name === "" || name !== path.basename(name) || !files.has(name)) {' 'if (name === "" || !files.has(name)) {' \
  && expect_green "$T_ASSETS" "the basename check is not what stops a path from leaving the directory"
restore

mp_mutation "control: the limit is not applied, and the test that raises the limit still sees overlap"
apply_mutation "$WORKER" '    maxConcurrentActivityTaskExecutions: settings.renderConcurrency,' '' \
  && expect_green "$T_CONC --testNamePattern=control" "the overlap probe sees overlap whatever the limit"
restore

use_pytest
mp_mutation "control: only the first of the two version checks is removed (the second still refuses)"
apply_mutation "$LAYOUT" 'if dub.script_version != ref.version or dub.script_key != ref.script_key:' 'if False:' \
  && expect_green "$P_LAYOUT" "either version check alone still refuses a track made for another script"
restore

mp_mutation "control: the fallback that accepts a precomposed letter by its parts is removed (the font has every Vietnamese letter whole)"
apply_mutation "$LAYOUT" 'if len(parts) > 1 and all(ord(p) in self._cmap for p in parts):' 'if False:' \
  && expect_green "$P_LAYOUT" "the decomposition fallback is not what covers Vietnamese"
restore

mp_mutation "control: the caption shadow is removed, which is not text the measurement looks at"
apply_mutation "$VIDEO" '      textShadow: "0 2px 6px rgba(0, 0, 0, 0.75)",' '' \
  && expect_green "$P_PARITY" "the parity measurement reads the white text pixels, not the shadow"
restore

mp_finish

# --- the frame is bounded by area, not only by side ------------------------------------------------------
mp_mutation "the spec model checks each side but not the pixels together"
apply_mutation "$MODELS" '        if self.width * self.height > MAX_FRAME_AREA:' '        if False:' \
  && expect_red "$P_LAYOUT" "test_the_spec_model_enforces_the_same_limits"
restore

mp_mutation "the layout accepts a frame with too many pixels"
apply_mutation "$LAYOUT" '    if ingest.width * ingest.height > MAX_FRAME_AREA:' '    if False:' \
  && expect_red "$P_LAYOUT" "test_a_video_too_large_for_the_renderer_is_refused_for_good"
restore

use_jest
mp_mutation "the renderer's schema check accepts a frame with too many pixels"
apply_mutation "$SPECTS" '    if (spec.width * spec.height > MAX_FRAME_AREA) {' '    if (false) {' \
  && expect_red "$T_CONTRACT" "each side within its limit but too many pixels together"
restore

use_pytest
mp_mutation "the renderer states another area limit than the model"
apply_mutation "$SPECTS" 'export const MAX_FRAME_AREA = 1920 * 1088;' 'export const MAX_FRAME_AREA = 1920 * 1080;' \
  && expect_red "$P_DRIFT" "test_the_area_limit_the_renderer_checks_is_the_one_the_model_has"
restore

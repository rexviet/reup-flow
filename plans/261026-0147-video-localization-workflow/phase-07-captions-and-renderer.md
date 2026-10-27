---
phase: 7
title: "Phụ đề và renderer (Remotion)"
status: in-progress
priority: P1
effort: "2d"
dependencies: [3, 4]
---

# Phase 07: Phụ đề và renderer

<!-- Updated: Red Team 2026-10-26 - HTTP localhost cho file tạm, key render theo hash, OTel TS, RENDER_CONCURRENCY, font parity kiểm bằng hành vi, chỉ nối với phase 06 ở phase 08 -->

## Goal

Làm được hai việc, test bằng fixture:
- Bước 4b ra `EditSpec`.
- Bước 7 render một mp4 duy nhất cho mỗi `spec_sha256`.

Việc nối với audio thật từ phase 06 làm ở phase 08, nhờ đó phase này chạy song song được với 05–06.
Remotion dùng license miễn phí (≤3 người, theo xác nhận của người dùng).

## Files to Create / Modify

**Python**
- `src/localization/activities/layout_captions.py`
- `assets/fonts/BeVietnamPro-SemiBold.ttf` (OFL). Đường dẫn đọc từ `CAPTION_FONT_PATH` ở cả hai phía.

**Renderer (Node)**
- `services/renderer/package.json`. Dependency:
  - `@temporalio/worker`, `@temporalio/activity`, `@temporalio/interceptors-opentelemetry`
  - `@opentelemetry/sdk-node`, `@opentelemetry/exporter-trace-otlp-http`
  - `@remotion/bundler`, `@remotion/renderer`, `remotion`, `react`
  - `ajv`, `@aws-sdk/client-s3`
- `services/renderer/src/worker.ts`: queue `render`, `maxConcurrentActivityTaskExecutions=RENDER_CONCURRENCY`.
- `services/renderer/src/tracing.ts`
- `services/renderer/src/activities/render.ts`
- `services/renderer/src/asset-server.ts`
- `services/renderer/src/edit-spec.ts`
- `services/renderer/src/remotion/Root.tsx`, `LocalizedVideo.tsx`
- `services/renderer/Dockerfile`: image pin digest, Chromium pin, font copy từ `assets/fonts`.

**Compose và test**
- `docker-compose.yml`: thêm `worker-render`, `shm_size`, env allowlist.
- `services/renderer/tests/edit-spec.contract.spec.ts`
- `services/renderer/tests/render.integration-spec.ts`
- `services/renderer/tests/mutations/phase-07.sh` và dòng `EXPECT`.
- `tests/activities/test_layout_captions.py`
- `tests/integration/test_font_parity.py`

## Tasks & Steps

1. **`layout_captions(script_key, script_version, subtitle_key, video_meta, audio_key, audio_sha256)`.**
   - **Hộp che:** `region` nới thêm 6%. Nếu `None` thì đặt sub ở vùng an toàn và không có hộp che.
   - **Ngắt dòng:** đo bằng PIL với font tại `CAPTION_FONT_PATH`. Tối đa 2 dòng, font từ 48 xuống 32px.
     Vẫn không vừa thì tách cue.
   - **Output:** EditSpec mang `script_version` và `audio_sha256`. Ghi lên `work/{id}/r{round}/edit-spec.json`.
2. **Renderer.**
   1. Bundle Remotion **một lần** lúc khởi động.
   2. Mỗi activity:
      1. Tải spec, validate bằng `ajv`.
      2. Tính `spec_sha = sha256(canonical spec)`.
      3. Nếu `render-{spec_sha}.mp4` đã tồn tại thì trả key luôn. Key chứa hash, nên không cần
         sidecar và không có trường hợp ghi không nguyên tử.
      4. Tải source và audio về thư mục `mkdtemp`.
      5. **`asset-server` phục vụ thư mục này qua `http://127.0.0.1:<port ngẫu nhiên>/`**, có token
         đường dẫn ngẫu nhiên cho mỗi job.
      6. `inputProps` dùng URL của server đó (D12).
      7. `renderMedia` với `onProgress` gọi heartbeat. Mỗi lần gọi tăng counter `render_invocations{spec_sha}` (dùng cho AC3).
      8. Upload `render-{spec_sha}.mp4`.
      9. Trong `finally`: tắt server, xoá thư mục tạm.
3. **Composition.**
   - `<OffthreadVideo muted src={url}>`, `<Audio src={url}>`.
   - Hộp che là `<AbsoluteFill>`. Mỗi cue là một `<Sequence>`.
   - Không có hiệu ứng zoom hay chỉnh màu.
4. **Tracing.** `OpenTelemetryActivityInboundInterceptor` cộng exporter OTLP tới Langfuse. Span
   `render` phải nằm trong cùng trace với các span Python (G3).
5. **Contract.**
   - Test Node validate 3 fixture. Các fixture này **do Python sinh lại trong CI** bằng `python -m localization.contracts.fixtures`.
   - Mutation drift nằm ở harness Python (phase 03).
6. **Font parity bằng hành vi.**
   - Render một cue cố định qua renderer thật.
   - Dùng `ffmpeg` cắt frame, tìm bounding box của pixel chữ.
   - So với độ rộng đo bằng PIL. Sai lệch phải ≤ 4px.

## Verification

- `uv run pytest tests/activities/test_layout_captions.py tests/integration/test_font_parity.py`
- `pnpm --filter @reup-flow/renderer test && pnpm --filter @reup-flow/renderer test:integration`
  - Render fixture 3 giây bằng **video HEVC thật từ Douyin**, đọc qua asset server.
  - Độ dài output khớp spec ±1 frame. Pixel hộp che đúng màu. Có stream audio.
  - Render lại cùng spec thì `render_invocations` không tăng.
- Test overlap `render` với `RENDER_CONCURRENCY=1`, kèm control.
- **Mutation:** `bash scripts/run-mutations.sh renderer/07` đỏ với:
  - Đổi config font ở một phía.
  - Bỏ kiểm tra key theo hash.

## Risk

Chromium trong Docker cần `shm_size`. Đặt sẵn trong compose.

## Rollback

Dừng `worker-render`. Workflow chờ ở bước 7.

## Tiến độ

**Trạng thái: in-progress.** Code, test tự động (mutation: chờ lần chạy cuối), image Docker và một lần render thật trong container đã xong và xanh. Chưa đóng phase vì chưa chạy với stack compose đầy đủ (`worker-render` chưa được `docker compose up` cùng Temporal và MinIO thật; Python workflow chưa gọi `render`, việc nối là của phase 08) và vì span `render` chưa được kiểm trong một trace do Python khởi tạo (chỉ kiểm bằng client/workflow TS; đường Python sang TS đã chứng minh ở G3 của phase 00, chưa chạy lại).

**Đã làm và đã kiểm**
- `activities/layout_captions.py` (queue `media`): đo bằng PIL với `CAPTION_FONT_PATH`, tối đa 2 dòng, cỡ 48 xuống 32 (bước 4), vẫn không vừa thì tách cue theo từ, thời gian chia theo độ dài chữ, không hở không chồng. Hộp che là vùng sub cũ nới 6% mỗi cạnh (kẹp trong khung); `region=None` thì không có hộp che và chữ ở vùng an toàn. Cue hiện từ đầu segment tới hết slot hoặc hết thời gian đọc (cái nào dài hơn), không lấn cue sau, không quá cuối video. Từ chối không retry (`ScriptDubMismatch`) nếu track dựng từ phiên bản script khác hoặc một dòng đọc khác với dòng viết. Ghi `work/{id}/r{round}/edit-spec.json`, trả `EditSpecRef` (key, `spec_sha256`, `script_version`, `audio_sha256`).
- Đo chữ ở cỡ x32 rồi chia lại: FreeType ở cỡ thật làm tròn từng advance. Ink đo bằng bitmap (PIL `getbbox` trả cả bearing). Trên Chromium, độ rộng chữ vẽ ra lệch dưới 1 px so với PIL.
- `services/renderer`: worker TS queue `render` (`maxConcurrentActivityTaskExecutions = RENDER_CONCURRENCY`, mặc định 1), `OpenTelemetryActivityInboundInterceptor` cộng exporter OTLP tới Langfuse, bundle Remotion một lần lúc khởi động, activity `render` (validate bằng `ajv` trên schema sinh từ Pydantic, hash canonical, trả key nếu `render-{sha}.mp4` đã có, tải source/audio/font về `mkdtemp`, `asset-server` loopback cổng ngẫu nhiên và token đường dẫn ngẫu nhiên mỗi job, heartbeat theo `onProgress`, upload, `finally` tắt server và xoá thư mục). Kiểm sha256 của audio với `audio_sha256` trong spec và tên font với `spec.font`.
- Composition: `OffthreadVideo muted`, `Audio`, hộp che là `AbsoluteFill`, mỗi cue một `Sequence`, không hiệu ứng.
- Hợp đồng: `contracts/edit-spec.schema.json` sinh lại. Test Node sinh lại 3 fixture từ Python mỗi lần chạy (`python -m localization.contracts.fixtures`), kiểm ajv chấp nhận, hash khớp hash Python (gồm dấu NFD, emoji, dấu nháy), và 10 kiểu spec sai bị từ chối không retry.
- Font parity bằng hành vi (`tests/integration/test_font_parity.py`): layout thật, renderer thật (Chromium), ffmpeg cắt frame, đo pixel chữ, lệch tối đa 4 px so với PIL.
- Render thật (`render.integration-spec.ts`, MinIO thật với user `renderer` chỉ trên `work/*`): video HEVC 1080x1920 cắt 3 giây từ một clip Douyin thật trong `data/` (transcode sang HEVC, vì các clip tải về đều là H.264), số frame và độ dài khớp spec trong ±1 frame, có stream AAC, pixel hộp che đúng màu (độ mờ 100 để màu chính xác), ảnh nằm ngoài hộp che, chữ trắng nằm trong, cue chưa tới thì không hiện chữ, render lại cùng spec không tăng `render_invocations` và không upload lại.
- Overlap: với `RENDER_CONCURRENCY=1`, 3 render do một workflow phát cùng lúc có đỉnh chồng nhau là 1; control (giới hạn 3) thì là 3. Tracing: span `RunActivity:render` nằm trong trace của client; control không gắn interceptor thì không có.
- Image Docker: base pin digest, Chrome Headless Shell tải trong image và dừng build nếu sha256 sai (cả x64 và arm64), `pnpm install --frozen-lockfile`. Chạy thật trong container (arm64, user `node`): render 3 giây ra mp4 H.264 và AAC.
- Compose: `worker-render` (`shm_size: 512m`, `mem_limit: 6g`, `/tmp` tmpfs 1g (cỡ đã đo, xem Sau review), `stop_grace_period` dài hơn shutdown 10 phút của worker, env allowlist, không port). `worker-media` mount `assets/fonts` và nhận `CAPTION_FONT_PATH`, `COVER_COLOR`, `COVER_OPACITY`. `compose-constraints.spec.ts` mở rộng; `downloader:infra` thêm 6 mutation (shm, key của localization lọt vào renderer, RENDER_CONCURRENCY, checksum Chrome, font hai phía khác nhau, `/tmp` vượt phần bộ nhớ còn lại).
- Mutation: **đã đăng ký, chưa chạy lại ở trạng thái cuối** (theo quyết định của người dùng, mutation chạy một lần sau khi xong cả 10 phase). Harness `renderer:07` có 55 mutation và 5 control, `downloader:infra` 22 và 2, đã khai trong `EXPECT`. Trong lúc phát triển, trước quyết định đó, harness `renderer:07` đã chạy xanh ở 37/37 mutation (lần đầu bắt được 5 vấn đề của chính harness: mẫu tên test phải đứng ngay sau tiền tố của dòng FAILED; mutation bỏ `server.close` làm jest treo; một kiểm tra phiên bản bị kiểm tra khác che nên mutation đơn lẻ không đỏ, đã đổi thành mutation đôi và thêm control) và, sau review, 55/55 với 5 control; bản chạy toàn bộ cuối cùng bị dừng giữa chừng, cây nguồn đã kiểm sạch sau đó. Bằng chứng mutation cuối cùng: chờ lần chạy cuối.
- Không đổi code workflow nên không ghi lại history replay; các history cũ vẫn replay xanh trong `uv run pytest`.

**Khác với phase file**
- Thư mục test của renderer là `tests/` (khớp `harness_dir` của runner), không phải `test/`.
- **Renderer không đọc được `source/`** (policy MinIO chỉ cho `work/*`, theo plan §6). Thay vì nới quyền, `layout_captions` sao chép video nguồn sang `work/{id}/r0/source.mp4` bằng `copy_object` phía server (không có byte nào đi qua tiến trình) và `EditSpec.source_key` trỏ vào bản sao. Cần `ObjectStore.copy`.
- `EditSpec` được mở rộng so với phase 03: `cover.color`, `cover.opacity_pct` (số nguyên, để hash không có số thực mà Python và JS viết khác nhau), `caption_box`, `Caption.font_px`. Lý do: renderer không được tự suy ra cỡ chữ, vị trí hay màu, và màu hộp che phải nằm trong hash của key render. Nên `COVER_COLOR`, `COVER_OPACITY` chỉ do worker-media đọc; worker-render chỉ nhận `CAPTION_FONT_PATH` (plan §6 ghi cả hai).
- `spec.font` là tên file (basename) của `CAPTION_FONT_PATH`; renderer từ chối nếu tên khác font của nó.
- Hash của spec do cả hai bên tính cùng một cách (`contracts/spec_hash.py`, `edit-spec.ts`); test parity so với hash Python.
- Ngoài danh sách file: `contracts/fixtures.py`, `contracts/spec_hash.py`, `EditSpecRef`, `ObjectStore.copy`, `config.py`, `workers/run.py`, `object-store.ts`, `remotion-render.ts`, `frames.ts`, `config.ts`, `cli.ts` (render cục bộ cho test font parity), `jest-protocol.js`, `pnpm-workspace.yaml` (`allowBuilds` của `@swc/core`, `protobufjs`), CI (job `renderer`, node và pnpm cho job `localization`), `.env.example`, `pillow` trong `pyproject.toml`.
- Chrome được image pin bằng URL của remotion.media và sha256, không phải gói `chromium` của Debian.

**Chưa kiểm được (nói thẳng)**
1. Chưa `docker compose up worker-render` trong stack đầy đủ; chỉ chạy image đó bằng `docker run` với `cli.js`, chưa chạy worker với Temporal và MinIO thật trong container.
2. Chưa kiểm cross-language span: Python workflow gọi activity TS và span `render` cùng trace Langfuse (AC7 phụ thuộc, phase 09/10).
3. `shm_size`: render thử trong container với `/dev/shm` mặc định 64 MB cho clip 3 giây vẫn chạy; 512m là phòng cho video dài hoặc nhiều tab (đo được ~40 MB dùng trên clip 60 s).
4. Độ rộng chữ đo trên một font; video 60 s thật đã render trong container để đo bộ nhớ (xem Sau review) nhưng chỉ một clip.

**Giới hạn đã biết**
- Một từ dài hơn hộp chữ không tách được: đặt riêng ở 32 px và tràn hộp.
- Hộp che thấp hơn hai dòng chữ thì chữ tràn ra ngoài hộp (vẫn đọc được vì có bóng chữ).
- Hộp che là tĩnh, phủ suốt video, không theo thời điểm sub cũ xuất hiện.
- Test font parity cần node, pnpm, Chromium; đặt `LOC_SKIP_RENDERER=1` để bỏ qua trên máy không có (thiếu thì fail chứ không skip).
- `render.integration-spec` để lại vài object nhỏ dưới `work/` của MinIO (user renderer không có quyền xoá; lifecycle 30 ngày).
- Counter `render_invocations` nằm trong bộ nhớ tiến trình (và là thuộc tính của span `render`), chưa là metric xuất ra ngoài; phase 10 cần đọc nó bằng span trên Langfuse.
- Hợp đồng input/output của activity `render` (`{aweme_id, spec_key}` và `{render_key, spec_sha256}`) mới có ở phía TS; phase 08 thêm model Python khi nối.

## Sau review

Sáu mục Medium đầu và M7 đã sửa. Các guard mới có mutation đã đăng ký trong `phase-07.sh` và `phase-infra.sh` (chưa chạy lại ở trạng thái cuối); test thường, lint, typecheck và `test:integration` đã chạy xanh.

**Đã sửa**
- **Giới hạn đầu vào của renderer.** `EditSpec` có trần: mỗi cạnh 4096 px, **diện tích khung tối đa 1920x1088** (cỡ mà bộ nhớ đã đo được cấp cho; Pydantic, `check_renderable` và ajv kiểm, vì JSON Schema không diễn tả được tích hai cạnh), 120 fps, 15 phút, 2000 caption. `layout_captions` từ chối không retry (`VideoOutOfBounds`, `InvalidEditSpec`) khi file vượt trần. Renderer có deadline cho một lần render (`RenderDeadline`, không retry) **tính theo độ dài video**: 5 phút cộng 6 giây cho mỗi giây video (đo được khoảng 3 giây tường cho mỗi giây video, nên dư gấp đôi); video 15 phút được 95 phút. Deadline rơi giữa lúc tải hay upload cũng là `RenderDeadline`, không phải lỗi retry và dừng được: tín hiệu huỷ của activity và deadline đi tới `renderMedia` qua `cancelSignal`.
- **Font xác định bằng byte.** `EditSpec.font_sha256`; layout tính, renderer so và từ chối không retry (`FontMismatch`) nếu khác, cả trong activity lẫn `cli.ts`. Test parity đo pixel thêm: font cùng tên khác byte bị từ chối, không có file ra.
- **Ký tự font không có** (你, emoji, ...): layout kiểm cmap bằng `fontTools` (một chữ có sẵn dạng dựng sẵn hoặc đủ phần tách đều được coi là có). **Quyết định: từ chối không retry (`UncoveredGlyphs`) và nêu id segment và ký tự**, không cờ-và-vẫn-render. Lý do: ô trống đốt vào video là lỗi người xem thấy mà không ai sửa được sau; text là của Localizer nên stage chạy lại là `localize`. Hệ quả cho phase 08: một bản dịch giữ nguyên tên Trung hay emoji sẽ làm run dừng ở bước này; phase 08 phải định tuyến lỗi này về Localizer hoặc `needs_human`, và có thể muốn prompt Localizer cấm ký tự ngoài tiếng Việt.
- **Dùng chung và huỷ.** Hai yêu cầu cùng spec trong một tiến trình chỉ có một `renderMedia` (single-flight). Render dùng chung thuộc về mọi người gọi: nó có `AbortController` riêng và chỉ dừng khi người gọi cuối cùng đã đi; mỗi người gọi tự đua với tín hiệu huỷ của mình và tự heartbeat bằng context của mình; người gọi cuối đợi render dọn xong rồi mới trả lỗi; test chồng nhau có control (hai spec khác nhau thì chồng nhau). Heartbeat định kỳ 5 s suốt tải, render và upload, không chỉ khi Remotion báo.
- **Bộ nhớ, có đo.** Render 60 s 1080x1920 HEVC trong image (`--memory=4g`): với mặc định của Remotion (số tab theo CPU của máy chủ 10 nhân, cache video theo RAM của máy chủ) container bị OOM-kill sau 20 s. Đặt 2 tab và cache video 512 MB: ở 4 GB vẫn bị OOM-kill (sau ~130 s, bộ nhớ leo tới 3.9 GiB); ở 6 GB chạy xong (exit 0), đỉnh 4.1 GiB resident, 52 MB `/tmp`, khoảng 40 MB `/dev/shm`, 182 s tường cho 60 s video (container 6 GB, exit 0). Compose: `mem_limit: 6g`, `/tmp` tmpfs 1g, `shm_size: 512m`; `compose-constraints.spec.ts` kiểm `mem_limit >= 4200 MB + tmpfs + shm` và có mutation.
- **Hộp che.** Vùng sub phát hiện cao hơn 35% chiều cao khung hoặc phủ hơn 40% diện tích bị từ chối không retry (`ImplausibleCover`, stage chạy lại là `detect_subtitles`); hộp chữ cao tối thiểu hai dòng 48 px (120 px), mở rộng quanh hộp che khi hộp che mỏng.
- **403 khi HEAD.** `exists()` của renderer và `exists`/`metadata` của `ObjectStore` Python coi 403 là "không có" (AWS S3 trả 403, không phải 404, khi principal chỉ có `ListBucket` theo tiền tố); 403 khi đọc vẫn là lỗi. Có test với client giả; chưa kiểm trên AWS S3/R2 thật.
- Plan §6: `COVER_*` chỉ ở worker-media. File list của phase file ghi `tests/`.
- Lows sửa: asset server đóng file khi client ngắt giữa chừng (`pipeline`, có test đếm fd); JSON hỏng là lỗi không retry; `r0/source.mp4` luôn được sao chép lại (nguồn tải lại không bị render cũ); layout từ chối khởi động khi Pillow thiếu raqm.

**Giới hạn đã biết (Low, ghi nhận)**
- Deadline và các trần là hằng số trong code, chưa cấu hình được qua env.
- `CopyObject` chỉ thử với moto (moto không áp IAM); thử với policy localization thật ở phase 08.
- Không có `.dockerignore` ở gốc repo; apt không ghim version; `curl` và `unzip` còn trong image chạy.
- Chưa có `spec_version`: thêm field vào `EditSpec` làm mọi hash đổi, và image renderer cũ từ chối spec mới (`InvalidEditSpec`). Thêm trước lần đổi field đầu tiên sau khi chạy thật.
- Xoay hình (metadata rotate) và VFR chưa thử; codec lạ là lỗi retryable của Remotion.
- Một segment cùng `start_ms` với segment sau không có caption; một span rất ngắn tách nhiều mảnh cho cue dưới một frame.
- Single-flight chỉ trong một tiến trình: hai replica `worker-render` vẫn có thể render cùng một spec cùng lúc (tốn CPU, kết quả giống nhau và ghi nguyên vẹn). Replica nhân giới hạn `RENDER_CONCURRENCY` và bộ nhớ lên.
- Số đo bộ nhớ trên một clip, một máy (Docker VM 8 GB, 10 nhân).
- Từ review lần hai (ghi nhận, chưa sửa): 403 khi HEAD bị coi là "không có" nên một 403 thật (khoá hết hạn, policy đổi) chỉ lộ ra khi upload, sau một lần render đầy đủ; nếu store thật trả 403 thì dùng `ListObjectsV2` với `Prefix` và `MaxKeys=1`, kiểm trên S3/R2 thật trước phase 10.
- `r0/source.mp4` luôn được sao chép lại nhưng spec không mang hash của nguồn: nguồn tải lại với hình khác mà cùng kích thước, fps, độ dài và cùng audio sẽ cho cùng spec và renderer trả mp4 cũ. Sửa đúng là đưa ETag hoặc sha256 của nguồn vào spec cùng `spec_version`.
- Lỗi vĩnh viễn của layout và renderer (`UncoveredGlyphs`, `ImplausibleCover`, `VideoOutOfBounds`, `InvalidEditSpec`, `FontMismatch`, `RenderDeadline`) được định tuyến ở phase 08 (xem quyết định ở đó).
- ffprobe: `avg_frame_rate` là `0/0` thì rơi về `r_frame_rate`, có thể là timebase kiểu `90000/1` và file hợp lệ bị từ chối là quá 120 fps; `nan`/`inf` trong `format.duration` ném lỗi retryable không bao giờ qua (lý thuyết, ffprobe in `N/A`). Có từ trước.

## Cover khớp chữ

Vùng phụ đề giờ là đồng thuận của các box model ở `detect_subtitles` (xem phase 04), đã nới 13% chiều cao và 2% chiều rộng. `grow_cover` chỉ cộng lề cố định 0.5% chiều rộng frame (5 px ở 1080) và đảm bảo rộng tối thiểu 60% chiều rộng frame, căn giữa quanh chữ (chỉ nới khi cần cho phụ đề Việt dài hơn dòng gốc). Cover được vẽ là chính hộp phụ đề (`caption_box_for`, tối thiểu 120 px cho hai dòng), nên chữ không lòi ra ngoài nền đen. `check_cover_plausible` giữ nguyên (35% chiều cao, 40% diện tích). Mutation `renderer:07` có thêm mục cho chiều rộng tối thiểu và cho cover vẽ bằng hộp phụ đề, đã đăng ký, chưa chạy.

---
phase: 7
title: "Phụ đề và renderer (Remotion)"
status: pending
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
- `services/renderer/test/edit-spec.contract.spec.ts`
- `services/renderer/test/render.integration-spec.ts`
- `services/renderer/test/mutations/phase-07.sh` và dòng `EXPECT`.
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

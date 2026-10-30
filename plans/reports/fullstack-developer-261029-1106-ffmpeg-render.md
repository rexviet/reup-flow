# Render bằng ffmpeg trong service localization (Phase A)

Ngày 2026-10-29. Trạng thái: xong, còn vài việc cho controller (xem cuối).

## Kết quả

Activity `render` giờ là code Python, chạy trong process `worker-media`. Nó giữ nguyên tên, task queue `render`, input `{aweme_id, spec_key}` và output `{render_key, spec_sha256}`, nên workflow và các history replay không đổi. Service Node/Remotion (`worker-render`, `services/renderer`) đã bị xoá.

Với fixture thực tế (67 s, 1080x1920, hình chuyển động `testsrc2`, 14 phụ đề có dòng đôi và dấu tiếng Việt), thời gian `render_file` (vẽ phụ đề và encode) là:
- trên máy macOS (ffmpeg 8.1): 7,3 s và 9,0 s;
- trong image `reup-flow-worker-media` (ffmpeg 5.1 của Debian bookworm, chạy qua VM Docker): 15,8 s và 18,5 s.

Để so sánh, Remotion mất 217 s chỉ để vẽ khung hình.

Tôi đã so từng pixel với Remotion trên cùng một spec, khi `services/renderer` chưa bị xoá. Phần ngoài caption box giống hệt nhau, chênh lệch bằng 0. Trong caption box, chữ của ffmpeg thấp hơn chữ của Chrome 1 px, còn lại chỉ khác ở mép glyph: chênh trung bình 2–3 mức xám trên 255. Phụ đề bật và tắt ở đúng cùng các frame 0/29/30/65/66, và cả hai bản đều có 90 frame.

## File

Mới:
- `services/localization/src/localization/activities/render.py`: activity, hàm `render_file`, lệnh ffmpeg, deadline và kiểm tra font.
- `services/localization/src/localization/activities/caption_images.py`: dùng Pillow vẽ mỗi phụ đề thành ảnh PNG, rồi tạo một luồng ảnh dạng concat.
- `services/localization/tests/activities/test_render.py`: 27 test.

Sửa:
- `services/localization/src/localization/workers/run.py`: role `media` đăng ký thêm queue `render` với giới hạn `RENDER_CONCURRENCY` (mặc định 1). Tracing interceptor đi theo client, giống các worker khác.
- `services/localization/tests/workers/test_agent_wiring.py`: kiểm tra `render` được phục vụ trên queue `render` với giới hạn đúng, và mọi cặp (activity, queue) trong các recording đều có worker thật phục vụ.
- `services/localization/tests/integration/test_font_parity.py`: vẫn đi qua layout thật, nhưng giờ render bằng code Python. Test so độ rộng nét chữ vẽ ra với độ rộng `FontMeter` đo, sai số tối đa 4 px. Test cũng kiểm tra font cùng tên nhưng khác byte thì bị từ chối (`FontMismatch`). Không cần pnpm nữa.
- `services/localization/tests/test_edit_spec_schema_drift.py`: bỏ test đọc `edit-spec.ts`. Giới hạn diện tích khung hình giờ được kiểm tra trong `test_render.py`.
- Chỉ sửa comment hoặc docstring đã sai: `layout_captions.py`, `contracts/{fixtures,spec_hash,ids,models,export}.py`.
- `docker-compose.yml`: xoá `worker-render` và anchor `x-renderer-s3-env`. Bỏ `RENDER_S3_*` khỏi `minio-init`. Thêm `RENDER_CONCURRENCY` cho `worker-media`.
- `infra/minio/init.sh`: xoá user và policy `renderer`. Render dùng user `localization`, user này đã có quyền đọc/ghi `work/*`.
- `.env.example`: bỏ `RENDER_S3_*` và sửa các comment liên quan.
- `services/downloader/test/compose-constraints.spec.ts`: bỏ các test riêng của `worker-render`. Thêm `RENDER_CONCURRENCY` vào allowlist của `worker-media`. Thêm test: không còn service `worker-render`, và không service nào nhận `RENDER_S3_*`.
- `.github/workflows/ci.yml`: xoá job `renderer`, bước cài Node trong job localization, và `RENDER_S3_*` ở bước minio-init.
- `pnpm-lock.yaml`: cập nhật bằng `pnpm install --lockfile-only`, không sửa tay.

Xoá: toàn bộ `services/renderer/`.

## Hành vi của renderer cũ

Giữ nguyên:
- Key `work/{aweme_id}/render-{spec_sha256}.mp4`. Nếu key đã có thì trả về ngay, không tải gì. Test xoá source rồi gọi lại để chứng minh điều này.
- Spec được kiểm tra bằng chính model `EditSpec` (schema cũ cũng sinh ra từ model này). Hash được tính bằng đúng `spec_sha256` mà layout dùng. Test chạy trên cả 3 mẫu trong `contracts.fixtures`.
- Các lỗi không retry, cùng `type` như trước: `InvalidInput` (spec_key nằm ngoài `work/{id}/`), `InvalidEditSpec` (JSON hỏng, sai giới hạn, quá diện tích khung hình, source hoặc audio thuộc video khác), `FontMismatch` (khác tên hoặc khác byte), `AudioHashMismatch`, `RenderDeadline`. Spec bị thiếu báo `ArtifactMissingError`.
- Deadline tăng theo độ dài video. Có heartbeat trong suốt quá trình. Khi bị huỷ, ffmpeg (cả process group) bị kill, và activity chờ thread dừng hẳn rồi mới xoá thư mục tạm. Thư mục tạm luôn bị xoá.
- Hình: nền đen; video gốc tắt tiếng phủ kín khung (`scale` với `force_original_aspect_ratio=increase` rồi `crop`, theo đúng fps của spec), sau khi source hết thì hình đen; khung che `drawbox` theo màu và `opacity_pct`; phụ đề căn giữa trong `caption_box`, dòng giữ đúng như tách bằng `"\n"`, đúng `font_px`, line-height 1.25, chữ trắng, bóng lệch 2 px và mờ Gaussian sigma 3 với alpha 0,75; frame theo `Math.round` (nửa frame thì làm tròn lên), mỗi phụ đề hiện ít nhất 1 frame, phụ đề sau vẽ đè phụ đề trước. Âm thanh chỉ có track lồng tiếng, được `apad` cho đủ độ dài hình. Số frame đúng `ceil(duration*fps/1000)`.
- Encode: libx264 `veryfast`, crf 20, yuv420p, AAC 192k, `+faststart`.

Cố ý bỏ hoặc đổi:
- Bỏ việc gộp hai lần render cùng spec trong một process. Temporal với concurrency 1 hiếm khi gặp trường hợp này. Nếu có gặp thì chỉ tốn thêm CPU, vì key là hash và object luôn được ghi trọn vẹn.
- Bỏ `RenderInvocations` và dòng log JSON của `PhaseTimer`. Thay bằng một dòng log `fetch_s/draw_s/total_s` và các attribute `render.spec_sha256`, `render.reused`, `render.draw_s` trên span. Heartbeat không còn gửi kèm tỉ lệ tiến độ.
- Deadline đổi từ 5 phút + 6 s cho mỗi giây video thành 5 phút + 2 s cho mỗi giây video. Mức đo được khoảng 0,13–0,28 s cho mỗi giây video, nên vẫn còn dư nhiều.
- ffmpeg thoát với mã khác 0 thì lỗi là `PermanentProviderError`, giống quy ước của `run_ffmpeg`. ffmpeg bị kill bởi signal (ví dụ OOM) thì lỗi là transient, có retry.
- Không còn user S3 riêng chỉ có quyền `work/*`. Render giờ dùng user `localization`, quyền rộng hơn: đọc `source/*`, ghi `output/*`. Đây là hệ quả của việc chạy render trong `worker-media`.

## Test

- `cd services/localization && uv run pytest -q tests/activities/test_render.py tests/integration/test_font_parity.py tests/workers/ tests/test_edit_spec_schema_drift.py tests/activities/test_layout_captions.py tests/test_default_caption_font.py tests/test_dockerfile.py tests/test_secret_canary.py tests/test_contract_hardening.py tests/test_worker_settings.py`: 235 passed.
- `uv run ruff check` và `uv run ruff format --check` trên 13 file Python đã đụng: sạch.
- `cd services/downloader && pnpm exec jest test/compose-constraints.spec.ts`: 21 passed, 1 failed. Test lỗi là `each added service receives exactly its own environment`, do `worker-agents`. Lỗi này có từ trước, không phải do thay đổi lần này: compose của `worker-agents` có `LOCALIZER_MODE`, `DUBBER_MODE`, `*_REASONING`, `REVIEWER_JEV_MODEL`, `REVIEWER_MODE`, `LOUDNESS_LUFS`, `TRUE_PEAK_DBTP` và `CAPTION_FONT_PATH`, nhưng allowlist chưa có. Tôi không sửa vì nằm ngoài phạm vi.
- Không chạy mutation test, không chạy video thật, không restart service.

## Việc còn mở cho controller

1. Các harness mutation vẫn nhắc tới renderer đã xoá. `services/downloader/test/mutations/phase-infra.sh` sẽ hỏng vì nó copy `services/renderer/Dockerfile` và sửa `*renderer-s3-env`. Trong `scripts/run-mutations.sh` còn mục `renderer:07` và `services/renderer/src` trong `TRACKED_PATHS`. `scripts/test-run-mutations.sh` cũng có nhắc tới. Tôi chưa sửa vì anh nói sẽ tự thêm mutation sau.
2. MinIO đang chạy vẫn còn user `renderer` cũ, vì `init.sh` chỉ thêm user chứ không xoá. Có thể xoá bằng `mc admin user remove local renderer`. Biến `RENDER_S3_*` trong `.env` thật giờ không còn được dùng.
3. Cần rebuild và restart `worker-media`, đồng thời gỡ container `worker-render` cũ (`docker compose up -d --remove-orphans`). Image worker-media hiện tại chạy được code mới; tôi đã kiểm tra bằng một container tạm chạy `--network none`.
4. Còn nên giữ `contracts/edit-spec.schema.json`, `contracts/export.py` và `contracts/fixtures.py` không? Người dùng schema duy nhất (renderer TS) đã bị xoá. Tôi giữ lại vì `tests/mutations/phase-03.sh` còn tham chiếu tới chúng, và `fixtures` giờ được test hash của render dùng.
5. Comment trong `workflows/video_localization.py` (gần `RENDER`) vẫn nói về deadline 95 phút của renderer cũ. Tôi không sửa vì file đó thuộc agent khác.

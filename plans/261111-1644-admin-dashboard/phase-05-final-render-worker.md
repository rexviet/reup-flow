---
phase: 5
title: "Worker render bản cuối trên máy"
status: completed
priority: P1
effort: "11h"
dependencies: [1]
---

# Phase 5: Worker review trên máy (render bản cuối, quét, mở Studio)

## Goal

Một worker pm2 trên máy (`reup-review-worker`) làm bốn việc:
render MP4 cho review project đã duyệt và upload MP4/SRT lên MinIO; quét mismatch và
chỉnh sửa chưa lưu của các project đang chờ duyệt; mở Studio theo yêu cầu từ dashboard;
ghi heartbeat kèm dung lượng đĩa và tình trạng VoiceStudio.

## Context

- Review project: HEAD ở `review/<aweme>/g<gen>/head.json` trong MinIO; manifest theo revision và asset bất biến (`review_bridge/repository.py:56-82`). Thư mục `data/review/<aweme>/g<gen>/` là bản chiếu để Studio mở.
- `Repository.projection` (`repository.py:179-190`) tính dấu vân tay trên mọi file `.json`, gồm `renders/*.meta.json` do Studio Export ghi. Hậu quả: sau mỗi lần export, project bị coi là có chỉnh sửa chưa lưu (`repository.py:224-231`), và `service.py:82-86` còn kéo các file meta này vào `studio_files`. Project `data/review/7689288513854595771/g3` đang ở đúng tình trạng này. <!-- red-team #2 -->
- Mismatch tính theo cue ở `review_bridge/models.py:33`.
- CLI: `hyperframes render [DIR] -o <file> -q delivery` (v0.8.77, `services/localization/spikes/hyperframes-review`).
- Env trên máy: `scripts/lib/host-voice-env.mjs` chỉ lấy PATH/HOME/LANG từ shell, phần còn lại từ env compose của `worker-agents` (`LOC_DATABASE_URL`, `LOC_S3_*`, S3 qua `127.0.0.1:9000`). `BridgeSettings` dùng tiền tố `REVIEW_` và gốc `/review` của container (`review_bridge/settings.py:14-24`), nên worker không dùng nó. <!-- red-team #14 -->
- Mở Studio: `scripts/review-studio.mjs` chỉ cho một project mở (`:309`); `scripts/review-auto-open.mjs` đã có logic sao lưu rồi thay Studio.
- pm2 mặc định chỉ đợi khoảng 1,6 giây khi dừng; các app hiện có đặt `kill_timeout` riêng (`ecosystem.config.cjs:23,36`). <!-- red-team #8 -->

## Sửa review_bridge trước

1. `Repository.projection` bỏ qua thư mục `renders/`; `service.py` không đưa file trong `renders/` vào `studio_files`.
2. Marker cũ: khi dấu vân tay tính theo cách cũ khớp `.projection.json`, ghi lại marker theo cách mới (một lần, trong `materialize` và trong bước quét). Không đụng thư mục có chỉnh sửa thật.
3. Test hồi quy: export (ghi `renders/x.meta.json`) xong thì quét báo `dirty=false` và `materialize` không lỗi.
4. Rebuild image `review-bridge` khi triển khai.

## Render

Worker **không** render từ thư mục Studio. Nó dựng bản chiếu của đúng `approved_revision`
từ asset bất biến trong MinIO ra một thư mục tạm ngoài `data/review` (thêm hàm
`Repository.export_revision(aweme, gen, revision, dest)` dùng chung logic staging của
`materialize`). Vì thế Studio đang mở, bridge đang ghi hay chỉnh sửa chưa lưu đều không
ảnh hưởng bản render, và không cần cổng "Studio đang mở". <!-- red-team #2, #5, #6 -->

Vòng lặp mỗi 5 giây:

1. Nếu dung lượng trống của ổ chứa `data/` dưới `REVIEW_WORKER_MIN_FREE_GB` (mặc định 5) thì không nhận việc, ghi lý do vào heartbeat. <!-- red-team #8 -->
2. Claim **một** dòng, gắn token mới:
   `UPDATE final_renders f SET state='rendering', attempts=attempts+1, claim=gen_random_uuid(), lease_expires_at=now()+interval '45 min' FROM review_decisions d WHERE (f.aweme_id,f.generation)=(SELECT ... FROM final_renders WHERE (state='queued' OR (state='rendering' AND lease_expires_at<now())) AND attempts<3 ORDER BY ... LIMIT 1 FOR UPDATE SKIP LOCKED) AND d.aweme_id=f.aweme_id AND d.generation=f.generation AND d.decision='approved' AND d.approved_revision=f.approved_revision RETURNING f.*, f.claim`. <!-- red-team #5, #8 -->
3. Đọc HEAD; nếu revision của HEAD khác `approved_revision` thì `failed` với lý do "Bản đã đổi sau khi duyệt, duyệt lại bản mới". <!-- red-team #5 -->
4. `export_revision` ra thư mục tạm.
5. Render bằng `hyperframes render <dir> -o <file> -q delivery` (FPS theo composition) trong process group riêng, <!-- Updated: Validation Session 1 - chất lượng delivery --> env tối thiểu (`PATH`, `HOME`, `LANG`, `TMPDIR`; không có credential DB hay S3), timeout cứng 30 phút (ngắn hơn lease 45 phút), gia hạn lease mỗi 5 phút (`WHERE claim = <token>`). Hết giờ thì kill cả process group. <!-- red-team #8, #10 -->
6. Upload `output/review/<aweme>/g<gen>/<revision>.mp4` và `.srt` (từ `captions.srt`). Render lại cùng revision sẽ ghi đè cùng key; đó là cùng nội dung.
7. Ghi `rendered`, `final_key`, `srt_key`, `rendered_at` với `WHERE claim = <token> AND state='rendering'`. Lần claim cũ hoặc đã bị Trả về thì không ghi được. <!-- red-team #8 -->
8. Lỗi → `failed`, `error` rút gọn (cùng điều kiện `claim`). Sau 3 lần thì dừng; admin bấm Render lại.
9. `finally`: xóa thư mục tạm.

## Quét, mở Studio và heartbeat

- Mỗi 60 giây quét các run `ready_for_review`/`needs_human`: đọc HEAD và manifest, đếm mismatch, so dấu vân tay thư mục local với marker để đặt `dirty`, upsert `review_status` (kèm `revision`). Chỉ đọc, không materialize.
- Mỗi vòng lấy `studio_open_requests` đang `pending`: chạy helper Node `scripts/review-studio-request.mjs <aweme> <gen>` dùng lại `replaceStudio` (đã export, `scripts/review-auto-open.mjs:56`, sao lưu rồi dừng Studio cũ) rồi mở project, ghi `done` hoặc `failed` kèm lỗi.
- Sửa `openNext` của `reup-review-auto-open` (`scripts/review-auto-open.mjs:40-54`): chỉ mở project mới khi không có Studio nào đang mở; không bao giờ thay Studio đang mở, để không đóng project admin vừa mở từ dashboard. Project mới vẫn hiện "Chờ duyệt" trên dashboard. Cập nhật test của `openNext`. <!-- Updated: Validation Session 1 - auto-open chỉ mở khi Studio trống -->
- Heartbeat mỗi vòng: upsert `host_workers('review-worker', now(), {disk_free_bytes, disk_total_bytes, voicestudio: {ok, latency_ms, checked_at}, current})`. VoiceStudio probe `{VOICESTUDIO_URL}/.well-known/voicestudio-speech` từ máy, như `scripts/start-all.sh:53,85`. <!-- red-team #15 -->
- SIGTERM: dừng nhận việc, đợi render hiện tại tối đa 60 giây, rồi trả dòng về `queued` (theo `claim`) và kill process group.

## Kiểm chứng render khớp Studio (làm trước)

Chạy tay `hyperframes render` trên bản chiếu tạm của một revision thật đã có export từ
Studio và so sánh: thời lượng, độ phân giải, có phụ đề, font và viền đúng. Ghi lại cờ hoặc
biến môi trường CLI cần, và xác nhận Chrome chạy **có** sandbox (ghi cờ khởi chạy vào
`docs/review-companion.md`). Nếu không khớp thì dừng và báo người dùng trước khi làm tiếp. <!-- red-team #10 -->

## Files

- Modify: `services/localization/src/localization/review_bridge/{repository,service}.py` và test
- Create: `services/localization/src/localization/review_worker/{__init__,main,final_render,review_scan,studio_requests,heartbeat}.py` và `services/localization/tests/review_worker/*`
- Create: `scripts/review-worker.mjs` (khởi chạy Python bằng `hostEnv`, truyền thêm `REVIEW_WORKER_DATA_ROOT` là đường dẫn tuyệt đối tới `data/review`, `FINAL_RENDER_WORKERS`, `REVIEW_WORKER_MIN_FREE_GB`), `scripts/review-studio-request.mjs` <!-- red-team #14 -->
- Modify: `scripts/review-auto-open.mjs` (`openNext` không thay Studio đang mở) và `scripts/tests/review-auto-open.test.mjs`
- Modify: `ecosystem.config.cjs` (app `reup-review-worker`, `kill_timeout: 90000`), `scripts/start-all.sh` (thêm vào `HOST_APPS`, hiện trong `status`) <!-- red-team #8, #14 -->
- ObjectStore của worker dựng từ `LOC_S3_*` (identity `localization`, đã có Get/Put trên `review/*` và Put trên `output/*`, `infra/minio/init.sh:52-53`)

## Verification

- Unit test: hai worker không claim trùng; lease hết hạn thì claim lại nhưng dừng ở 3 lần; kết quả của lần claim cũ không ghi đè; Trả về giữa chừng thì kết quả bị bỏ; HEAD đổi sau khi duyệt → `failed`; dòng `final_renders` không có quyết định `approved` tương ứng thì không bao giờ được claim; đĩa dưới ngưỡng thì không nhận việc; thư mục tạm luôn bị xóa.
- Test scan: manifest có cue lệch → `mismatch_count` đúng; thư mục chỉ có `renders/*.meta.json` mới → `dirty=false`.
- Test yêu cầu mở Studio: `pending` → `done`; lỗi → `failed` kèm lý do.
- Thử thật: duyệt một project trong khi Studio vẫn mở project đó → render xong, MP4 trong MinIO phát được, SRT khớp phụ đề.
- `pm2 stop reup-review-worker` → badge worker chuyển `down` trong 2 phút; dòng đang render trở về `queued`.
- Tắt VoiceStudio → badge VoiceStudio `down` trong một chu kỳ heartbeat.

## Risks

- Render nặng (mỗi worker HyperFrames mở một Chrome, khoảng 256 MB). `FINAL_RENDER_WORKERS` mặc định 2; render tuần tự từng video.
- Render chạy cùng lúc với Studio làm máy chậm hơn; chấp nhận được với một admin.

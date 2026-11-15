---
title: Cook admin dashboard phase 5 review worker
date: 2026-11-12
summary: "Worker pm2 reup-review-worker: render revision đã duyệt từ asset bất biến, quét review, mở Studio theo yêu cầu, heartbeat; sửa review_bridge để export không làm project bị coi là chưa lưu"
---

# Cook admin dashboard phase 5: review worker

## What happened
- `review_bridge`: `projection` bỏ `renders/`; `stage` (dùng chung với `materialize`) bỏ cả `studio_files` nằm dưới `renders/` (revision g3 thật đã lỡ chứa 3 file meta); `export_revision(aweme, gen, revision, dest)` dựng đúng revision ra thư mục rỗng; `marker_state`/`adopt_marker`/`local_state` nhận marker cũ (tính theo cách trước, giữ revision của nó) hoặc thư mục khớp từng byte với HEAD, không bao giờ chấp nhận chỉnh sửa thật.
- `review_worker/` (`config`, `final_render`, `review_scan`, `studio_requests`, `heartbeat`, `main`): vòng 5 giây; render một dòng mỗi lần trên thread riêng; quét 60 giây; yêu cầu mở Studio trên thread riêng (không làm trễ heartbeat); heartbeat kèm đĩa, VoiceStudio, `current`, `paused_reason`.
- Claim bằng `FOR UPDATE OF c SKIP LOCKED` có join `review_decisions` (đúng revision, `approved`), token `claim`, lease 45 phút gia hạn 5 phút, timeout cứng 30 phút, tối đa 3 lần; mọi ghi đều `WHERE claim = token AND state = 'rendering'`. Lỗi tạm thời đưa về `queued` kèm lý do tới lần thứ 3 rồi `failed`; "HEAD đã đổi" là `failed` ngay. SIGTERM đợi 60 giây rồi `REQUEUE_SQL` (trả lại lượt thử).
- Node: `scripts/review-worker.mjs` (+ `lib/review-worker-env.mjs`), `scripts/review-studio-request.mjs` (dùng lại `replaceStudio`, khởi chạy launcher tách rời), `openNext` của auto-open không còn thay Studio đang mở (test cập nhật), `ecosystem.config.cjs` (`kill_timeout: 90000`, `treekill: false`), `start-all.sh` (`HOST_APPS`). Docs: mục mới trong `docs/review-companion.md`.

## Verification
- Render thật của `7689288513854595771/g3` (189 s) so với bản Studio export: cùng 1024x576, 30 fps, 189,67 s, AAC, khung có phụ đề giống nhau (SSIM khoảng 0,98); chỉ khác bitrate vì `-q delivery`. Mất 2 phút 10 giây.
- Chạy worker thật (không qua pm2) với project `7691886043717039013/g7` trong lúc Studio của người dùng vẫn mở project đó: render xong, MP4 1922x1080, 203,07 s giải mã được, SRT giống hệt `captions.srt` của Studio. Đã xóa dòng quyết định/render thử và dòng heartbeat; hai object `output/review/7691886043717039013/g7/14a935c0fc4646b9bccb6dfa365c547d.{mp4,srt}` (khoảng 259 MB) còn lại trong MinIO vì cả hai identity không có quyền xóa.
- `pm2 start --only reup-review-worker` rồi `pm2 stop` giữa lúc render: dừng sau khoảng 63 giây, dòng về `queued` với `attempts=0`, không còn tiến trình hyperframes/Chrome/ffmpeg của worker, thư mục tạm trống, heartbeat ngừng (badge `down` sau 2 phút theo ngưỡng của health). Sau đó `pm2 delete`. Không tắt VoiceStudio để thử badge; probe có test với transport giả.
- Postgres tạm cổng 5599 (đã xóa): `tests/review_worker` + `tests/review_bridge` xanh, trừ `test_unimported_studio_source_is_not_discarded` (đã đỏ trước phase này: chuỗi `font:20px/28px` không còn trong HTML). Node: review-auto-open, review-studio-request, review-studio, host-voice-worker xanh.

## Decisions / gotchas
- Chrome do HyperFrames 0.8.77 khởi chạy với `--no-sandbox --disable-setuid-sandbox`, CLI không có cờ bật sandbox. Kế hoạch yêu cầu xác nhận Chrome có sandbox; điều đó không đạt, đã ghi vào docs và báo lại để người dùng chấp nhận rủi ro (chỉ nạp composition tự dựng, env tối thiểu không có credential).
- Quét review chỉ đọc, kể cả marker (lệch kế hoạch gốc "ghi marker trong bước quét"): review chỉ ra host và container bridge không có khóa file chung đáng tin cậy trên bind mount, scan ghi marker có thể đè marker bridge vừa ghi. Marker chỉ được ghi trong `materialize` và `save` (trong container).
- Review bắt: adopt marker cũ gắn nhầm revision HEAD (sửa: giữ revision của marker), pm2 treekill giết renderer trước khi worker kịp requeue (sửa: `treekill: false`, đã kiểm bằng `pm2 stop` thật), Studio chặn heartbeat (thread riêng), renew/ghi cuối chết khi DB rớt kết nối (`Session` mở lại một lần), approval không được kiểm trước khi upload (renew cũng kiểm approval), group tiến trình không bị kill khi leader đã thoát (kill group luôn, macOS trả EPERM cho group chỉ còn zombie), `clean_stale` chỉ xóa thư mục quá lease.
- Chưa làm: rebuild image `review-bridge` (container đang chạy code cũ nên export vẫn làm project dirty trong bridge cho tới khi rebuild), reload `reup-review-auto-open` đang chạy code `openNext` cũ, không mở Studio thật từ yêu cầu dashboard (sẽ thay Studio của người dùng đang mở).
- Vì render dài khi máy bận: render 203 s mất khoảng 8 phút khi chạy chung với bộ test.

## Next steps
Phase 6 (giao diện) đọc `review_status`, `final_renders`, `host_workers`; phase 7 cần rebuild bridge, reload auto-open, bật worker bằng `start-all.sh`, thử mở Studio thật và dọn hai object thử ở trên.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

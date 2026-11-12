---
phase: 5
title: "Thumbnail video"
status: pending
priority: P2
effort: "7h"
dependencies: [4]
---

# Phase 5: Thumbnail video

## Goal

Mỗi dòng video trong bảng pipeline và trang chi tiết hiện một thumbnail thật (hạng mục 1), lấy từ chính file MP4 nguồn đã nằm trong MinIO, không cần thêm request tới Douyin (quyết định D1, phương án b).

## Context

- Bảng `videos` có `duration_ms` và `play_addr` nhưng **không có** trường ảnh bìa; `aweme_list` được giữ nguyên dạng gốc (`services/downloader/src/parser/parser.client.ts:15`). Lưu ảnh bìa Douyin cần thêm request tới CDN Douyin, đi ngược việc giảm request của plan trước (ghi chú F1 ở `videos.play_addr`).
- Image downloader **không có ffmpeg** (`services/downloader/Dockerfile`); máy host có ffmpeg (`/usr/local/bin/ffmpeg` trong shell hiện tại) vì HyperFrames render cần nó (`final_render.py:180` dọn tiến trình ffmpeg). Nhưng `hostEnv` rơi về `PATH=/usr/bin:/bin:/usr/sbin:/sbin` (`scripts/lib/host-voice-env.mjs:21`), có thể không chứa `/usr/local/bin`: spike phải kiểm trong môi trường pm2 thật.
- Identity MinIO `localization` có `PutObject` trên `output/*` và đọc `source/*`; identity `downloader` có `GetObject` trên `output/*` (`infra/minio/init.sh:43,56`). Nên một đối tượng `output/thumbs/<aweme>.jpg` ghi và đọc được mà không đổi policy, và không cần bảng DB.
- Role `localization` chỉ `SELECT (aweme_id, sec_user_id, storage_path)` trên `videos` (`261026-outbox-starter-grants.sql:15`): **không đọc được** `status` và `duration_ms`. Worker chọn ứng viên bằng cột đã được cấp và lấy độ dài bằng `ffprobe` từ chính file.
- **Luồng worker:** quét chạy đồng bộ trong vòng chính cùng heartbeat (`main.py:95-123`); chỉ Studio request và render có luồng riêng. Việc nặng như tải nguồn và ffmpeg phải ở luồng riêng, nếu không heartbeat quá 2 phút và dashboard báo worker chết (`dashboard.health.ts:42`).
- **Đường link ký hiện hành:** `MEDIA_KINDS = ['source','final','srt']` nằm ở `services/downloader/src/dashboard/media-token.ts:3` (không phải `dashboard.schemas.ts`), regex xác thực token cứng ba kind và bắt buộc có generation (`media-token.ts:45-46`), `resolveMediaKey` coi mọi kind khác `source` là bản render (`final_key` trừ khi là `srt`, `dashboard.media.ts:127-133`), `open()` cứng `video/mp4` hoặc SRT, controller luôn gửi `Cache-Control: private, no-store` (`dashboard.media.controller.ts:35`), `mediaLinksSchema` nhận đúng một `aweme_id` (`dashboard.schemas.ts:57-63`) và mỗi lần ký ra một URL mới.
- Danh sách video poll mỗi 3 giây (`useDashboard.ts:15`).

## Thiết kế

- **Sinh ảnh (worker):** luồng daemon riêng với kết nối DB riêng (mẫu `studio_thread`), mỗi lượt xử lý **một** video, bỏ qua lượt khi đang render, kiểm `stopping` giữa các video và nằm trong ngân sách join lúc tắt. Ứng viên: video có `storage_path` mà chưa có đối tượng `output/thumbs/<aweme>.jpg` (kiểm bằng HEAD trên MinIO), **mới nhất trước** (theo `aweme_id` giảm dần nếu spike xác nhận ID tăng theo thời gian; nếu không thì theo `localization.runs`/thứ tự khác worker đọc được), **một video mỗi lượt**, không quét cả kho một lần. Video cũ có ảnh dần theo thời gian; không có đợt làm hàng loạt lúc khởi động. Đây là xấp xỉ của "video đang hiển thị": worker không biết trang nào đang mở, và thêm tín hiệu nhu cầu (bảng hoặc cờ) bị loại để khỏi thêm migration. <!-- Updated: Validation Session 1 - làm dần video cũ, mới nhất trước --> Video cắt lỗi được nhớ trong bộ nhớ tiến trình và thử lại tối đa một lần mỗi lần khởi động worker, để không lặp vô hạn trên file hỏng.
- **An toàn ffmpeg:** tải file nguồn vào `mkstemp` dưới thư mục làm việc của worker (quyền 0600), kiểm đủ chỗ trống đĩa, kiểm chữ ký ISO-BMFF (`ftyp` ở byte 4-8), **không bao giờ đưa URL cho ffmpeg**. Chạy `ffmpeg -nostdin -protocol_whitelist file -format_whitelist mov,mp4,m4a,3gp,3g2,mj2 -ss <t> -i <file> -frames:v 1 -vf scale=320:-2 -q:v 5 <out.jpg>` với timeout 20 giây, `try/finally` dọn file tạm. Thời điểm cắt: khoảng giây thứ 1, hoặc 10% độ dài (từ `ffprobe`) nếu video ngắn hơn. Đường dẫn `ffmpeg` truyền tường minh qua `FFMPEG_BIN` ở `review-worker-env.mjs` (đường dẫn tuyệt đối, không dựa vào PATH).
- **Lưu:** tải JPEG lên `output/thumbs/<aweme>.jpg` bằng identity `localization`. Không có migration.
- **Phục vụ:** `thumb` là kind thứ tư của link ký, **không gắn generation** (dùng generation `0` trong token, khóa đối tượng dựng từ aweme đã kiểm). `resolveMediaKey` có nhánh `thumb` tường minh trả `output/thumbs/<aweme>.jpg`, và mã **đóng khi gặp kind lạ** (không rơi về `final_key`); `open()` trả `image/jpeg`; controller đặt `Cache-Control: private, max-age=600` cho `thumb`. Đối tượng chưa có thì trả 404 (MinIO trả 403 khi không có `ListBucket`, đã được map về 404), UI hiện khung giữ chỗ.
- **Link theo lô, ổn định:** thêm `POST /api/dashboard/media-links/batch { aweme_ids[1..100], kinds:['thumb'] }` chỉ ký (không kiểm tồn tại), cùng quy tắc API key. Hạn dùng token làm tròn theo cửa sổ 10 phút để URL ổn định trong cửa sổ, nên trình duyệt cache được; client xin link khi đổi trang và làm mới trước khi hết hạn, **không xin theo nhịp poll 3 giây**.
- **UI:** thumbnail 9:16 nhỏ cạnh mô tả, kèm thời lượng (đã có từ restyle); chưa có ảnh thì khung giữ chỗ trung tính, không dùng ảnh mẫu. Không thêm trường `has_thumb` vào danh sách (downloader không có `ListBucket`, và một HEAD cho mỗi dòng là N request).

## Files

- Create: `services/localization/src/localization/review_worker/thumbnails.py`; Modify `main.py` (luồng riêng, ngân sách shutdown), `config.py` (đọc `FFMPEG_BIN`, giới hạn)
- Modify: `scripts/lib/review-worker-env.mjs` (truyền `FFMPEG_BIN`), `scripts/review-worker.mjs` nếu cần
- Modify: `services/downloader/src/dashboard/media-token.ts` (`MEDIA_KINDS`, regex xác thực), `services/downloader/src/auth/api-key.guard.ts` (nhận token `thumb`), `dashboard.media.ts` (`resolveMediaKey`, content type), `dashboard.media.controller.ts` (cache per kind), `dashboard.schemas.ts` (schema lô)
- Modify: `services/web/src/dashboard/PipelineTable.tsx`, `VideoDetail.tsx`, `useMediaLinks.ts`, `services/web/src/api/client.ts`, `services/web/src/api/types.ts`
- Tests: `services/localization/tests/review_worker/` (ffmpeg giả, file hỏng, thiếu ftyp, dọn file tạm), `services/downloader/test/dashboard/`, `services/downloader/test/auth/every-route-requires-api-key.e2e-spec.ts` (route lô tự được khám phá)

## Tasks

1. Spike: trong môi trường pm2 thật, kiểm `FFMPEG_BIN` tuyệt đối chạy được; cắt thử một khung hình từ file nguồn thật; đo kích thước và thời gian; ghi vào journal.
2. `thumbnails.py` theo thiết kế an toàn ở trên, chạy trong luồng riêng; thêm vào ngân sách shutdown.
3. Mở rộng `MEDIA_KINDS` và regex trong `media-token.ts`; thêm nhánh `thumb` trong `resolveMediaKey`; content type và cache theo kind; đóng khi kind lạ.
4. Route lô `media-links/batch` có giới hạn 100; ổn định hạn token theo cửa sổ 10 phút.
5. UI xin link theo lô khi đổi trang, tái dùng đến gần hết hạn, hiển thị ảnh và khung giữ chỗ.
6. Test: file hỏng hoặc thiếu `ftyp` không chạy ffmpeg; file tạm luôn bị xóa kể cả khi ffmpeg lỗi; token `thumb` không bao giờ phân giải ra `final_key`; token hết hạn trả 401; link lô vượt 100 trả 400; luồng thumbnail không làm heartbeat trễ trong test với ffmpeg giả chậm.

## Verification

- Test Python, downloader và web xanh; route lô nằm trong test yêu cầu API key.
- Trên stack thật: sau một lượt worker, các video có `storage_path` có ảnh dưới `output/thumbs/`, trang chính hiện ảnh thật; heartbeat vẫn đập đều khi đang cắt ảnh.
- Đo: kích thước ảnh mỗi video ở mức chục KB.

## Risks

- Tải file nguồn lớn chỉ để lấy một khung hình: chấp nhận một lượt tải mỗi video, không phụ thuộc URL ký; nếu spike cho thấy quá chậm thì xem xét `ffmpeg` đọc qua tệp tạm với `-ss` trước `-i` (vẫn là tệp, không là URL).
- Worker chưa chạy thì chưa có ảnh: UI dùng khung giữ chỗ.
- Muốn đổi sang ảnh bìa gốc của Douyin sau này: hợp đồng ổn định là `output/thumbs/<aweme>.jpg` và `kind=thumb`.

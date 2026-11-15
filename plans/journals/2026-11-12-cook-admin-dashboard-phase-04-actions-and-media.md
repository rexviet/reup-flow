---
title: Cook admin dashboard phase 4 actions and media
date: 2026-11-12
summary: "POST /api/dashboard/actions (8 hành động, giao dịch riêng từng video) và media ký HMAC qua proxy S3 chỉ đọc; policy MinIO downloader thêm output/*"
---

# Cook admin dashboard phase 4: actions and media

## What happened
- `dashboard.actions.ts`: một route cho localize, rerun, approve, return, rerender, mark_published/unpublished, open_studio. Mỗi video một transaction, điều kiện nằm trong `WHERE` hoặc dưới khóa dòng `localization.runs`. Trả `{done, skipped[{aweme_id, reason}]}`, tối đa 100 id.
- `media-token.ts`, `dashboard.media.ts`, controller: `POST /media-links` (cần API key) và `GET /media/:token` (chữ ký thay API key, qua decorator `@SignedMedia`, guard sửa đúng một chỗ). `MediaReader` riêng, chỉ bucket cấu hình, dựng key lại từ DB với ba dạng được phép, stream có Range, 404 cho 403/NoSuchKey.
- Env `DASHBOARD_MEDIA_SECRET` (tùy chọn, tối thiểu 32 ký tự; thiếu thì media trả 503), compose, `.env.example`, đăng ký với redactor.
- `infra/minio/init.sh` và comment CI: downloader đọc thêm `output/*`. Đã áp policy lên stack local bằng `mc admin policy create` (không chạy lại vòng tạo user): đọc `output/` được, `review/` bị từ chối, ghi `output/` bị từ chối, user localization vẫn đọc được `source/`.

## Decisions
- `approve` chỉ một video mỗi lần: một `revision` không thể khớp nhiều video (review phát hiện, kế hoạch gốc thiếu).
- `rerun` yêu cầu không còn workflow mở nào của video (kể cả canary mà outbox chưa biết), `workflowStatus` của generation hiện tại không `running`, Temporal đọc được; một lần Temporal lỗi thì cả lô còn lại bỏ qua ngay.
- Khóa dòng `runs` rồi mới đọc các bảng khác: join dưới `FOR UPDATE` đưa cho giao dịch chờ dữ liệu cũ, test song song đã bắt lỗi này.
- Duyệt lại chỉ đưa render về `queued` khi render không `rendering`; nếu worker giành dòng giữa chừng thì rollback cả quyết định.
- `return` bị từ chối khi đã `published`; `mark_published` chỉ có hiệu lực một lần và giữ platform/link cũ.
- Content-Type suy từ loại tệp, không từ metadata object.

## Verification
- Review độc lập: 3 High (re-approve nửa vời, return video đã đăng, approve hàng loạt) và 5 Medium; đã sửa hết trừ HEAD/416 Content-Range và timeout socket 120s (low, chấp nhận).
- Postgres tạm cổng 5555 (đã xóa): jest dashboard + auth + env 298 xanh, actions spec chạy ba lần ổn định; tsc, eslint sạch. Toàn bộ downloader: còn đỏ các mục đã biết (compose-constraints, credential cứng 5432 ở db.module, health, lanes, cookie-leak, spike-skip-locked); compose-constraints đỏ do biến localization khác, không liên quan.
- Chưa chạy: HTTP vào container `nest` (code cũ, cần build lại và đặt `DASHBOARD_MEDIA_SECRET`), đọc MinIO thật qua downloader từ code Nest.

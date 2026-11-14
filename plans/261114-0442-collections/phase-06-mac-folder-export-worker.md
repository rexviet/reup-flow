---
title: "Phase 6: Worker lưu thư mục trên Mac"
status: completed
priority: P1
effort: "1d"
dependencies: [4]
---

# Phase 6: Worker lưu thư mục trên Mac

## Overview

`reup-review-worker` vai trò `studio` (chạy trên Mac) xử lý dòng `folder_exports`: tải bản cuối và SRT
(nếu có) từ MinIO về `REVIEW_WORKER_EXPORT_ROOT/<target_subdir>/<file_stem>.mp4|.srt`, ghi đè qua file
tạm rồi đổi tên.

## Key Insights

- Claim dùng trạng thái `exporting` có `claim` và `lease_expires_at`, cùng `FOR UPDATE SKIP LOCKED` trong
  subquery như `final_renders` (`review_worker/final_render.py:26-43`). Khóa chỉ cần sống trong một câu
  lệnh autocommit; thiếu `SKIP LOCKED` thì người claim thứ hai chờ khóa rồi chỉ kiểm lại `id` và lấy
  luôn dòng đó (sửa theo review phase 2, M2). <!-- red-team: claim -->
- Yêu cầu mở Studio chạy trên một thread riêng (`main.py:132-138`) với timeout 150 s
  (`studio_requests.py:23`); thread bị join chỉ 5 s khi tắt (`main.py:169`). Export tải hàng trăm MB nên
  phải có thread riêng.
- `final_key` do render worker trên Windows ghi qua LAN. Dashboard chỉ phát key sau khi kiểm tra tiền
  tố và ký tự (`dashboard.media.ts:133-146`); worker phải kiểm tra y như vậy. <!-- red-team: key -->

## Requirements

- Env mới `REVIEW_WORKER_EXPORT_ROOT` (tuyệt đối, đã tồn tại). Đích là `root/target_subdir`; thư mục
  con được tạo nếu chưa có, nhưng gốc thì không. <!-- quyết định: thư mục gốc -->
- Claim: `UPDATE … SET state='exporting', claim=…, lease_expires_at=now()+interval … WHERE id = (SELECT …
  WHERE state='pending' OR (state='exporting' AND lease_expires_at < now()) ORDER BY id LIMIT 1)
  RETURNING …`; settle chỉ khi `claim` khớp.
- Kiểm tra key: `final_key` và `srt_key` phải bắt đầu bằng `output/review/{aweme_id}/g{generation}/`
  và chỉ gồm `[A-Za-z0-9._/-]` không có đoạn `.` hay `..`; ngoại lệ duy nhất là `output/{aweme_id}.mp4`
  đã được phase 4 xác nhận. Key sai: `failed`.
- Ghi file an toàn:
  - Mở thư mục đích một lần bằng `O_DIRECTORY | O_NOFOLLOW`, kiểm `realpath` nằm trong gốc.
  - Tạo file tạm tên ngẫu nhiên `.reup-export-<uuid>.part` bằng
    `os.open(..., O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW, dir_fd=fd)`.
  - `os.replace(tmp, final, src_dir_fd=fd, dst_dir_fd=fd)`; lỗi giữa chừng thì xóa file tạm.
  - Tên file chỉ lấy từ `file_stem` (khớp `^(E\d{2,}-)?\d+$`), không có gì từ dữ liệu Douyin.
- SRT là tùy chọn khi `srt_key` NULL (video luồng cũ); `files` ghi đúng những file đã viết.
- Khi khởi động, xóa các file `.reup-export-*.part` cũ trong các thư mục của dòng `exporting` hết hạn.
- Kiểm tra dung lượng trống như `REVIEW_WORKER_MIN_FREE_GB`.

## Related Code Files

- Create: `services/localization/src/localization/review_worker/folder_exports.py`
- Modify: `services/localization/src/localization/review_worker/main.py`, `review_worker/config.py`
- Modify: `ecosystem.config.cjs` (env `REVIEW_WORKER_EXPORT_ROOT` cho `reup-review-worker`),
  `scripts/lib/review-worker-env.mjs`
- Create: `services/localization/tests/review_worker/test_folder_exports.py`

## Implementation Steps

1. Test trước, dùng MinIO giả hành xử như thật (thiếu key thì ném `ArtifactMissingError`) và thư mục
   tạm thật:
   - Tên file đúng có và không có tập; ghi đè bản cũ; SRT NULL chỉ ghi `.mp4`.
   - `target_subdir` thoát ra ngoài gốc (qua symlink) bị chặn; symlink đặt sẵn ở tên file tạm không ghi
     được ra ngoài.
   - Key sai tiền tố: `failed`, không tải.
   - Key thiếu: `failed`, không để file `.part`.
   - Hai worker claim cùng lúc: chỉ một xử lý (chứng minh có chồng nhau thật).
   - Lease hết hạn: dòng được claim lại; file `.part` cũ bị dọn.
   - Yêu cầu mở Studio vẫn được xử lý trong lúc đang export.
2. Viết handler, thread riêng, nối vào vòng `studio`.

## Todo

- [x] Test handler
- [x] Handler, claim, thread riêng, dọn file tạm
- [x] Env và cấu hình pm2

## Success Criteria

- `cd services/localization && uv run pytest tests/review_worker/test_folder_exports.py` xanh; kiểm kết
  quả bằng file thật trên đĩa.

## Security Considerations

- Worker chỉ ghi dưới `REVIEW_WORKER_EXPORT_ROOT`, nên ai có API key cũng không khiến Mac ghi ra ngoài
  thư mục gốc.

## Ghi chú triển khai (2026-11-14)

- `files` là đường dẫn tương đối so với thư mục gốc: `["<target_subdir>/<file_stem>.mp4", "….srt"]`, chỉ
  những file đã ghi xong (lỗi giữa chừng vẫn ghi phần đã xong). `error` là câu cố định, không bao giờ có
  đường dẫn máy Mac hay nội dung exception.
- Thay cho kiểm `realpath`: mở từng cấp thư mục bằng `O_DIRECTORY | O_NOFOLLOW` từ gốc, nên không có
  khoảng hở giữa kiểm và dùng. Symlink ở bất kỳ cấp nào làm request `failed`.
- Tải bằng `ObjectStore.download_fileobj` mới vào fd file tạm (`download_file` của boto3 tự tạo tên tạm
  riêng, không kiểm soát được `O_NOFOLLOW`). Bản cuối rỗng bị từ chối; SRT rỗng vẫn nhận.
- Dọn file `.reup-export-*.part` chạy ở đầu mỗi lượt `drain`, cho các dòng `exporting` hết hạn, không chỉ
  lúc khởi động: worker dừng giữa chừng khởi động lại khi lease còn sống, dòng được claim lại sau đó.
- `REVIEW_WORKER_EXPORT_ROOT` tùy chọn và chỉ bắt buộc là đường dẫn tuyệt đối. Không đặt thì request chờ
  (log cảnh báo). Thư mục chưa có (ổ ngoài chưa cắm) không chặn worker khởi động; mỗi request `failed`
  với "Thư mục gốc trên Mac không tồn tại." pm2 nhận biến từ shell chạy `start-all.sh`.
- Mọi lỗi không phải DB làm dòng `failed` (bấm lại để thử); lỗi DB để lease 30 phút hết hạn rồi claim lại.
  Lease gia hạn trước mỗi file; mất claim thì dừng, không settle.
- **Cho phase 8 (cần người dùng quyết):** user MinIO `localization` chỉ được ghi `output/*`
  (`infra/minio/init.sh:56`), không được đọc. Đã thử thật: `GetObject` `output/*.mp4` bằng credential đó
  trả `AccessDenied`. Worker báo "Worker không có quyền đọc output/ trên MinIO." Phải cấp `s3:GetObject`
  trên `output/*` cho `localization`, hoặc cho worker dùng credential riêng, trước khi chạy thật.
- Review: [code-reviewer-261114-1843-collections-phase-06](../reports/code-reviewer-261114-1843-collections-phase-06.md).

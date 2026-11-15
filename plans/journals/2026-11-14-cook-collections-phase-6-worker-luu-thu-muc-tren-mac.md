---
title: "Cook collections phase 6: worker lưu thư mục trên Mac"
date: 2026-11-14
summary: "Worker vai trò studio xử lý folder_exports trên thread riêng: claim bằng SKIP LOCKED và lease, tải bản cuối và SRT vào REVIEW_WORKER_EXPORT_ROOT qua file tạm O_NOFOLLOW rồi đổi tên; phát hiện user MinIO localization không đọc được output/."
---

# Cook collections phase 6: worker lưu thư mục trên Mac

## What happened

`review_worker/folder_exports.py` mới: claim một dòng `folder_exports` (`FOR UPDATE SKIP LOCKED` trong
subquery, trạng thái `exporting`, lease 30 phút), kiểm `file_stem`, `target_subdir` và khóa MinIO giống hệt
`resolveRenderKey` của dashboard, mở từng cấp thư mục dưới gốc bằng `O_NOFOLLOW`, tải vào
`.reup-export-<random>.part` (`O_CREAT|O_EXCL|O_NOFOLLOW`) rồi `os.replace` sang tên cuối. `files` ghi đường
dẫn tương đối, `error` là câu cố định. Worker chạy export trên thread `folder-exports`, nên Studio và
heartbeat không phải chờ. Env `REVIEW_WORKER_EXPORT_ROOT` đi từ shell qua pm2 vào `workerEnv`.

## Những chỗ đáng nhớ

- Key Insight của phase nói autocommit loại `SKIP LOCKED`, nhưng sai. Test chồng nhau thật giữ transaction
  đầu mở trong lúc người claim thứ hai chạy. Bỏ `SKIP LOCKED` thì người thứ hai chờ khóa, test đỏ sau 3 s.
- Dò trước khi code thì thấy user MinIO `localization` chỉ được ghi `output/*`. Đã gọi `GetObject` thật và
  nhận `AccessDenied`. Test bằng moto không có policy nên không bao giờ thấy lỗi này. Phase 8 cần người dùng
  chọn: cấp quyền đọc hoặc dùng credential riêng.
- `download_file` của boto3 tự tạo tên tạm cạnh đích, nên không giữ được `O_NOFOLLOW`. Phải thêm
  `ObjectStore.download_fileobj` để ghi vào fd do mình mở.
- Dọn `.part` chỉ lúc khởi động là không đủ: worker dừng giữa chừng khởi động lại khi lease còn sống, rồi
  chính nó claim lại dòng đó 30 phút sau. Chuyển việc dọn vào đầu mỗi lượt `drain`.
- Review bắt hai lỗi thật. Gốc đặt nhưng ổ chưa cắm làm worker không khởi động được, nên mất luôn heartbeat
  và Studio. Một thư mục tên giống file tạm (đặt được qua `save_subdir`) làm `unlink` ném `PermissionError`
  và chặn mọi export về sau. Cả hai được sửa sau khi có test đỏ trước.
- Thử thêm `S_ISREG` trước `unlink`, nhưng mutation sống sót: bắt `OSError` từng mục đã đủ. Đã bỏ cho gọn.
- Baseline đầu chạy cùng lúc đang sửa code, nên không dùng được. Chạy lại trong worktree HEAD sạch.
- Có 17 mutation, mỗi cái gỡ một guard (SKIP LOCKED, O_NOFOLLOW ở thư mục và file tạm, kiểm khóa, gia hạn
  lease giữa hai file, thread riêng, câu lỗi không lộ đường dẫn…). Tất cả đều làm test đỏ.

## Còn mở

- Quyền đọc `output/` của worker (chặn chạy thật ở phase 8).
- Nơi đặt `REVIEW_WORKER_EXPORT_ROOT` lâu dài: hiện lấy từ shell chạy `start-all.sh`, chưa đọc từ `.env`.

---
phase: 1
title: "S3Storage cho downloader"
status: pending
priority: P1
effort: "1d"
dependencies: [0]
---

# Phase 01: S3Storage cho downloader

<!-- Updated: Red Team 2026-10-26 - ngưỡng sweep riêng cho S3, test hai process, migrate có --revert, prefix bắt buộc -->

## Goal

Downloader ghi video qua S3 API, và vẫn giữ mọi bảo đảm hiện có của `Storage`:
- Commit nguyên tử.
- Kiểm số byte.
- `beforeCommit`.
- Dọn upload dở.

Đổi MinIO, S3 hay R2 chỉ là đổi config.

## Files to Create / Modify

- Create: `services/downloader/src/storage/s3.storage.ts`
- Modify: `services/downloader/src/storage/storage.module.ts`. Chọn backend theo `STORAGE_BACKEND`.
- Modify: `services/downloader/src/storage/storage.interface.ts`. Cập nhật doc:
  - `PutResult.path` có dạng `s3://bucket/key`.
  - `freeBytes()` trả `Infinity` với S3.
  - `removeStaleTemp` với S3 dựa trên `Initiated`, không dựa trên mtime.
- Modify: `services/downloader/src/config/env.ts`. Thêm các biến của plan.md §6. `S3_KEY_PREFIX` là **bắt buộc và khác rỗng**.
- Modify: `services/downloader/src/lanes/reaper.lane.ts`. Truyền `S3_STALE_UPLOAD_MS` khi backend là S3.
- Modify: `services/downloader/package.json`. Thêm `@aws-sdk/client-s3`.
- Modify: `docker-compose.yml`. Thêm env vào allowlist của `nest`.
- Create: `services/downloader/migrations/261026-storage-migration-map.sql` và `.down.sql`. Tên bảng không kèm schema.
- Create: `services/downloader/test/storage/s3.storage.integration-spec.ts`
- Create: `services/downloader/test/storage/s3.cross-process.integration-spec.ts`
- Create: `services/downloader/test/mutations/phase-11.sh`
- Modify: `scripts/run-mutations.sh`. Thêm dòng `11:5:0` vào `EXPECT` **trong cùng commit**.
- Create: `scripts/migrate-local-storage-to-s3.ts`. Có `--dry-run` và `--revert`.

## Tasks & Steps

1. **`put` (D3).**
   1. `CreateMultipartUpload`.
   2. `UploadPart` theo chunk 8MB, đếm byte trong lúc gửi.
   3. Stream lỗi hoặc số byte sai → `Abort` rồi throw.
   4. Gọi `beforeCommit()`. Trả `false` → `Abort` rồi trả `null`. Throw → `Abort` rồi throw lại.
   5. `CompleteMultipartUpload`.
   6. Trả `{ path: 's3://bucket/key', bytes }`.
2. **Registry in-flight.** Một `Set<uploadId>` trong process, xoá ra trong `finally`.
3. **`removeStaleTemp(olderThanMs)` (D4).**
   - Với S3, ngưỡng thực tế là `max(olderThanMs, S3_STALE_UPLOAD_MS)`, mặc định 24h.
   - Lý do: `Initiated` không thay đổi trong lúc upload còn chạy, khác với mtime. Registry lại
     không thấy được upload của process khác (script migrate, lúc rolling deploy).
   - Abort những upload quá ngưỡng và không nằm trong registry.
4. **Các phương thức còn lại.**
   - `exists`: `HeadObject`.
   - `remove`: `DeleteObject`. 404 không phải lỗi.
5. **Ghi chú cho plan downloader.** Thêm vào `plans/260824-2152-douyin-downloader/plan.md`:
   preflight đĩa ở phase 10 phải bỏ qua khi `freeBytes` là `Infinity`.
6. **Script migrate.**
   - **Backup DB trước.**
   - Chỉ chạy khi `nest` đang dừng lane (`LANES_ENABLED=false`).
   - Với mỗi video `done` có đường dẫn local:
     1. `S3Storage.put` với `expectedBytes` bằng kích thước file.
     2. Ghi `storage_migration_map(aweme_id, old_path, new_path, migrated_at)`.
     3. `UPDATE storage_path`.
   - **Không xoá file local** cho tới khi verify xong.
   - `--revert`: chỉ ghi lại `storage_path` cho các dòng có trong map. Không bao giờ restore cả DB.

## Verification

Chạy `pnpm --filter @reup-flow/downloader test:integration -- s3`. Mọi test kiểm **kết quả**, không kiểm lời gọi.

| Tình huống | Kỳ vọng |
|---|---|
| Commit bình thường | `GetObject` đúng số byte |
| Decline | Không có object, `ListMultipartUploads` rỗng |
| Stream bị cắt | Không có object, không còn upload dở |
| Upload đang sống trong cùng process | Sweep không đụng tới |
| Upload quá ngưỡng | Bị abort |
| **Hai process** | Process A upload chậm. Process B sweep với lease 5 phút. Upload của A vẫn complete được |

Các bước khác:
- `bash services/downloader/test/mutations/phase-11.sh`: 5 mutation đỏ.
- `bash scripts/run-mutations.sh 11`: đúng số lượng đã khai.
- `migrate-local-storage-to-s3.ts --dry-run`: số video khớp với câu SQL đếm.
- `--revert` trên một bản sao DB test trả lại đúng đường dẫn cũ.

## Risk

R2 có thể khác S3 ở `ListMultipartUploads`. Kiểm khi chọn provider production.

## Rollback

1. Chạy `--revert`.
2. Đặt `STORAGE_BACKEND=local`.

File local vẫn còn nguyên, vì script không xoá.

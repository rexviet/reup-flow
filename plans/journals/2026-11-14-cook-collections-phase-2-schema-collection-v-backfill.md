---
title: "cook collections phase 2: schema collection và backfill"
date: 2026-11-14
summary: "Bảng collections, folder_exports, collection_id cho video và glossary theo collection; cầu nối tối thiểu ở upsertMany và GlossaryStore"
---

# cook collections phase 2: schema collection và backfill

## Chuyện gì đã xảy ra

Phase 2 thêm hai migration: `261114-collections` (downloader) và `261114-glossary-collection-scope`
(localization). Mỗi profile được một collection kênh, mọi video được gán `collection_id` (NOT NULL).
Glossary đổi khóa từ `channel_id` sang `collection_id` và có FK tới `public.collections`. Bản down chọn
dòng của collection kênh trước, rồi đến dòng tạo sớm nhất, và báo bằng NOTICE số dòng đã bỏ.

## Vướng mắc

- Plan để phase 3 và phase 5 viết code gán collection và tra glossary. Nhưng `collection_id` NOT NULL và
  việc đổi tên cột glossary làm chính tiêu chí của phase 2 không thể xanh (bộ test downloader, `tests/agents`).
  Cách giải là một cầu nối tối thiểu. `upsertMany` tạo collection kênh (idempotent) và gán nó cho video mới.
  `GlossaryStore` đổi `channel_id` của lượt chạy thành id collection kênh ngay trong SQL.
- `apply-migrations.sh` từ chối mọi dòng bắt đầu bằng `BEGIN`, kể cả `BEGIN` của PL/pgSQL. Phải viết
  `DECLARE x bigint; BEGIN` trên một dòng, như migration 261112.
- Migration evidence của `261026-localization-core` sẽ báo "Partial schema" khi glossary đã đổi khóa. Giờ nó
  chấp nhận cả hai khóa. Đã thử bỏ nhánh này: test apply-migrations đỏ đúng chỗ.

## Review

Không có lỗi Critical hay High. Đã sửa, mỗi mục có test đỏ trước:
- unique index một phần cho yêu cầu lưu đang mở;
- CHECK chủ sở hữu của collection kênh và 合集;
- từ chối ký tự điều khiển trong đường dẫn;
- `file_stem` phải gắn với `aweme_id`;
- bỏ grant `REFERENCES`;
- câu claim trong test dùng `SKIP LOCKED`.

## Kết quả kiểm tra

- Downloader chạy tuần tự (`--runInBand`). Trước khi sửa (HEAD): 9 suite và 24 test đỏ. Sau khi sửa: 8 suite
  và 23 test đỏ, đều là lỗi môi trường có từ trước; không có lỗi mới. Số test tăng thêm 56.
- Localization: 440 test qua. Còn 1 test đỏ có từ trước (`test_localizer` thiếu `source_video_key`), không
  liên quan tới phase này.
- `apply-migrations.test.sh`: PASS.

## Bước tiếp

- Phase 3 thay cầu nối trong `upsertMany` bằng `resolveCollections`.
- Phase 5 thay phần tra glossary bằng `spec.collection.id`.
- Phase 6 sửa Key Insight về claim: cần `SKIP LOCKED`.
- Phase 8: DB 5433 chưa có ledger. Lần chạy thật sẽ áp 6 migration, không chỉ hai migration mới.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

---
title: Cook admin dashboard phase 1 data model
date: 2026-11-12
summary: "Hai migration mới (downloader + localization) có file down, đã áp trên DB thật; test quyền role localization và test down/up xanh"
---

# Cook admin dashboard phase 1: data model

## What happened
- Thêm `261112-dashboard-intake` (outbox `held`, `profiles.auto_localize`, `videos.localize_on_done`, `review_decisions`, `publications`, `studio_open_requests`, `dashboard_actions`) và `261112-review-delivery` (`final_renders`, `review_status`, `host_workers`), mỗi cái có file down.
- Nối vào `apply-migrations.sh`, `migration-evidence.sql`, `test/support/db.ts`, drizzle `schema.ts`; sửa fixture `videos.mapper.spec.ts` cho cột mới.
- `scripts/tests/apply-migrations.test.sh` mở rộng: down/up, `held` nhận và `bogus` bị chặn, down từ chối khi còn dòng `held`, role `localization` ghi được bảng của nó và bị từ chối (đúng lỗi `permission denied`) khi ghi `review_decisions`.
- Áp trên DB thật, backup: `backups/migrations-20261112-063807-78548.dump`.

## Decisions / gotchas
- Script từ chối dòng `BEGIN` đứng riêng, nên các khối `DO $$` viết `... BEGIN` cùng dòng với khai báo.
- GRANT cho `localization` bọc trong `IF EXISTS role` để schema test không cần role.
- Review bắt lỗi: test jest dùng connection pool bị kẹt transaction aborted sau down thất bại; đã chuyển sang client riêng có ROLLBACK. Test shell giờ đòi đúng lỗi `permission denied`.
- Không làm (ngoài spec, ghi lại): CHECK `approved` phải có `approved_revision`, unique pending cho `studio_open_requests`, `truncateAll` chưa gồm bảng mới.
- Down của downloader xóa dữ liệu bảng mới (chỉ dòng `held` được bảo vệ).
- `compose-constraints.spec.ts` fail sẵn từ trước (xác nhận bằng stash); `reviews.repository.spec.ts` thỉnh thoảng fail khi chạy cả bộ, pass khi chạy riêng.

## Next steps
Phase 2 (giữ video) và phase 3, 5 (song song được) dựa trên các bảng này.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

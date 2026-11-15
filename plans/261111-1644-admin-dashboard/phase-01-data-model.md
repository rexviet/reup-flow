---
phase: 1
title: "Data model và migration"
status: completed
priority: P1
effort: "4h"
dependencies: []
---

# Phase 1: Data model và migration

## Goal

Thêm đúng các cột và bảng dashboard cần, với migration có file down, áp qua
`scripts/apply-migrations.sh` (script này backup trước khi áp).

## Context

- Outbox: `services/downloader/migrations/261026-localization-outbox.sql` (CHECK `state IN ('pending','dispatched','rejected','dead')`).
- Starter chỉ claim `state = 'pending'` (`services/localization/src/localization/starter/outbox_starter.py:61-69`), nên trạng thái mới `held` không bị dispatch.
- `localization.runs` chỉ có dòng khi run kết thúc (`activities/publish.py:200`).
- Role: downloader và `apply-migrations.sh` chạy bằng `reup`, là **superuser** (`infra/postgres/create-databases.sh:56-57`, `workspace.repository.ts:55`). Downloader tiếp tục đọc `localization.*` bằng quyền superuser như hiện nay; plan không tuyên bố giảm quyền. Worker trên máy dùng role `localization` (NOSUPERUSER), nên mọi bảng worker ghi phải `OWNER TO localization` hoặc được GRANT tường minh. <!-- red-team #9 -->
- `apply-migrations.sh` gọi `pg_temp.migration_present(name)` từ `scripts/migration-evidence.sql`; tên lạ sẽ `RAISE EXCEPTION` (`migration-evidence.sql:56`). Script chỉ chạy file up. <!-- red-team #9 -->
- Test helper downloader hardcode `MIGRATION_NAMES` (`services/downloader/test/support/db.ts:20`) và yêu cầu SQL không qualify schema. <!-- red-team #9 -->

## Files

- Create: `services/downloader/migrations/261112-dashboard-intake.sql` và `.down.sql`
- Create: `services/localization/migrations/261112-review-delivery.sql` và `.down.sql`
- Modify: `scripts/apply-migrations.sh` (thêm hai file vào `MIGRATIONS`, sau `261108-review-project-key.sql`)
- Modify: `scripts/migration-evidence.sql` (thêm nhánh `WHEN` cho hai migration mới)
- Modify: `services/downloader/test/support/db.ts` (thêm `261112-dashboard-intake` vào `MIGRATION_NAMES`)
- Modify: `services/downloader/src/db/schema.ts` (drizzle cho cột và bảng mới của downloader)

## Schema

Downloader (schema mặc định, không qualify tên, owner `reup`):

- `localization_outbox.state`: thay CHECK, thêm `'held'`.
- `profiles.auto_localize boolean NOT NULL DEFAULT false`: công tắc tự localize theo kênh.
- `videos.localize_on_done boolean NULL`: ghi đè theo từng video (NULL = theo kênh).
- `review_decisions(aweme_id, generation, decision CHECK IN ('approved','returned'), approved_revision text, confirm_mismatch boolean NOT NULL DEFAULT false, note text, decided_at timestamptz, PRIMARY KEY (aweme_id, generation))`. Chỉ downloader ghi. <!-- red-team #5 -->
- `publications(aweme_id, generation, state CHECK IN ('unpublished','scheduled','publishing','published','failed') DEFAULT 'unpublished', platform text, post_url text, note text, updated_at, PRIMARY KEY (aweme_id, generation))`.
- `studio_open_requests(id bigserial PK, aweme_id, generation, state CHECK IN ('pending','done','failed') DEFAULT 'pending', error text, requested_at, handled_at)`: yêu cầu mở Studio từ dashboard, worker trên máy xử lý.
- `dashboard_actions(id bigserial PK, aweme_id text NOT NULL, action text NOT NULL, detail jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now())`, index `(aweme_id, created_at DESC)`.
- GRANT cho role `localization`: `SELECT` trên `review_decisions`; `SELECT, UPDATE (state, error, handled_at)` trên `studio_open_requests`. Không GRANT nào khác.

Localization (schema `localization`, mọi bảng `ALTER TABLE ... OWNER TO localization`, giống `261026-localization-core.sql:44-46`): <!-- red-team #9 -->

- `final_renders(aweme_id, generation, approved_revision text NOT NULL, state CHECK IN ('queued','rendering','rendered','failed'), attempts int NOT NULL DEFAULT 0, claim uuid, lease_expires_at, error text, final_key text, srt_key text, rendered_at, PRIMARY KEY (aweme_id, generation))`. Index partial trên `state = 'queued'`. Downloader chèn dòng `queued` khi duyệt; worker ghi phần còn lại. Worker chỉ render khi `review_decisions` có `decision='approved'` và cùng `approved_revision`, nên một dòng do pipeline tự chèn không vượt qua được cổng duyệt. <!-- red-team #5, #8 -->
- `review_status(aweme_id, generation, revision text, cue_count int, mismatch_count int, dirty boolean, checked_at, PRIMARY KEY (aweme_id, generation))`: worker ghi kết quả quét.
- `host_workers(name text PK, seen_at timestamptz, info jsonb)`: heartbeat của worker trên máy (đĩa, VoiceStudio, việc đang làm).

## Tasks

1. Viết hai migration và hai file down; mỗi file một cặp `BEGIN;`/`COMMIT;` đúng kiểu script yêu cầu. Down của downloader từ chối khi còn dòng `held`, không xóa dữ liệu.
2. Thêm vào `MIGRATIONS`, thêm nhánh evidence trong `migration-evidence.sql`, cập nhật `test/support/db.ts`.
3. Cập nhật drizzle `schema.ts`.
4. Trên DB test (`MIGRATION_TEST_DATABASE`): áp bằng script; rồi chạy down bằng `psql -1 -f <file>.down.sql`, xóa dòng ledger tương ứng trong `applied_migrations`, áp lại bằng script. Chứng minh đảo ngược được. <!-- red-team #9 -->
5. Test quyền: kết nối bằng role `localization` (không phải superuser), chạy đúng các câu upsert/update mà worker sẽ chạy trên `final_renders`, `review_status`, `host_workers`, `studio_open_requests`, và xác nhận bị từ chối khi ghi `review_decisions`. <!-- red-team #5, #9 -->
6. Áp trên DB thật bằng `scripts/apply-migrations.sh` (có backup). Ghi đường dẫn file backup vào báo cáo.

## Verification

- Script áp thành công trên DB test, down rồi up không lỗi.
- `INSERT` outbox `state='bogus'` bị CHECK chặn; `'held'` được nhận.
- Test quyền ở task 5 xanh.
- `pnpm --filter downloader test` xanh (test helper chạy migration mới).

## Risks

- Đổi CHECK trên bảng đang dùng: khóa ngắn, bảng nhỏ nên chấp nhận được. Tra tên constraint thật bằng `pg_constraint` trong migration, không đoán.

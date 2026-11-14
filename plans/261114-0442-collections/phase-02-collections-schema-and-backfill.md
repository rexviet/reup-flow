---
title: "Phase 2: Schema collection và backfill"
status: done
priority: P1
effort: "1.5d"
dependencies: [1]
---

# Phase 2: Schema collection và backfill

## Overview

Tạo bảng `collections`, gắn collection và số tập cho video, collection chọn tay cho job, bảng yêu cầu
`folder_exports`, cấp quyền cho role `localization`, và chuyển glossary từ khóa kênh sang khóa
collection. Backfill để mỗi kênh cũ có một collection mặc định.

## Key Insights

- `videos` không được có `job_id` (F7). `collection_id` là thuộc tính của video nên hợp lệ; ghi lý do
  vào comment của `schema.ts`.
- `nest` kết nối bằng superuser `reup` (`DATABASE_URL`), nên không cần grant cho `nest`. Starter và
  worker trên Mac, trên Windows dùng role `localization` (`LOC_DATABASE_URL`), vốn chỉ có
  `SELECT (aweme_id, sec_user_id, storage_path) ON videos`
  (`services/localization/migrations/261026-outbox-starter-grants.sql:15`). <!-- red-team: grant -->
- Migration áp trong khung bảo trì đã dừng mọi service (phase 8), nên không cần expand/contract.
- Danh sách migration viết cứng nằm ở nhiều nơi (xem Related Code Files). <!-- red-team: consumer -->

## Data Model

`collections` (schema public, chủ sở hữu là downloader):

| Cột | Kiểu | Ghi chú |
|---|---|---|
| `id` | uuid PK | `gen_random_uuid()` |
| `kind` | text | `channel`, `mix`, `manual` (CHECK) |
| `external_id` | text null | `sec_user_id` với `channel`, `mix_id` với `mix`, NULL với `manual` |
| `sec_user_id` | text null | kênh sở hữu, FK `profiles` |
| `name` | text | 1–200 ký tự, đã qua `cleanDisplayName` |
| `bgm_offset_db` | real null | −24 đến 24, bước 0.5 (giá trị nhị phân chính xác) |
| `caption_text_color` | text null | `^#[0-9a-f]{6}$` |
| `cover_color` | text null | `^#[0-9a-f]{6}$` |
| `cover_opacity_pct` | smallint null | 0–100, cùng đơn vị với `Cover.opacity_pct` trong spec <!-- red-team: opacity --> |
| `save_subdir` | text null | thư mục con dưới `REVIEW_WORKER_EXPORT_ROOT` trên Mac <!-- quyết định: thư mục gốc --> |
| `created_at`, `updated_at` | timestamptz | |

- `UNIQUE (kind, external_id)`; CHECK `external_id IS NULL` khi và chỉ khi `kind = 'manual'`.
- `save_subdir`: không bắt đầu bằng `/`, mỗi đoạn khác `.` và `..`, không có NUL, tối đa 512 ký tự.
- `videos.collection_id uuid NOT NULL REFERENCES collections(id)`; index `(collection_id, create_time DESC)`.
- `videos.mix_episode integer NULL CHECK (mix_episode > 0)`.
- `videos.collection_manual boolean NOT NULL DEFAULT false`: true khi collection do người dùng chọn
  (form tải hoặc chuyển tay); enumerate không bao giờ đổi collection của những video này.
- `jobs.collection_id uuid NULL REFERENCES collections(id)`.
- `folder_exports`: `id bigserial`, `aweme_id`, `generation`, `collection_id`, `target_subdir` và
  `file_stem` (chụp lúc yêu cầu, ví dụ `E01-7689288513854595771`), `final_key`, `srt_key` (NULL được),
  `state` (`pending`, `exporting`, `done`, `failed`), `claim uuid`, `lease_expires_at`, `error`,
  `files jsonb`, `requested_at`, `handled_at`. Index một phần trên `state IN ('pending','exporting')`.
- Grant cho `localization` (trong khối `IF EXISTS role` như `261112-dashboard-intake.sql:80-84`):
  `SELECT (collection_id, mix_episode) ON videos`; `SELECT, REFERENCES ON collections`; `SELECT` và
  `UPDATE (state, claim, lease_expires_at, error, files, handled_at) ON folder_exports`.

Glossary (`localization.glossary`, migration riêng bên localization):

- Đổi tên `channel_id` thành `collection_id`, đổi kiểu sang uuid theo collection kênh tương ứng, thêm
  FK tới `public.collections(id)` để ghi mồ côi bị từ chối. Dòng có `channel_id` không khớp kênh nào:
  migration dừng với `RAISE EXCEPTION` kèm số lượng, để người vận hành quyết định.
- Không thêm cột. Thuật ngữ tự đặt từ dashboard ghi `workflow_id = 'dashboard'` và
  `source_aweme_id = 'dashboard'`, giữ nguyên NOT NULL. <!-- red-team: down migration, scope -->
- Down: với mỗi cặp (kênh, `term_zh`), giữ dòng của collection kênh, nếu không có thì giữ dòng tạo sớm
  nhất; bỏ dòng thuộc collection tạo tay không có kênh; `RAISE NOTICE` số dòng bị bỏ; đổi lại
  `channel_id text` và PK cũ.

## Related Code Files

- Create: `services/downloader/migrations/261114-collections.sql` và `.down.sql`
- Create: `services/localization/migrations/261114-glossary-collection-scope.sql` và `.down.sql`
- Modify: `services/downloader/src/db/schema.ts`
- Modify: danh sách migration viết cứng:
  - `scripts/apply-migrations.sh` (`MIGRATIONS`)
  - `scripts/migration-evidence.sql` (nhánh `WHEN` cho hai migration mới; mục của
    `261026-localization-core` chấp nhận cả `channel_id` lẫn `collection_id`, nếu không sẽ báo
    "Unknown migration" hoặc "Partial schema")
  - `scripts/tests/apply-migrations.test.sh` (số migration ở `:13`, `:15`, `:34`; danh sách ở `:32`)
  - `services/downloader/test/support/db.ts` (`MIGRATION_NAMES`)
  - `services/downloader/test/support/localization-db.ts` (`LOCALIZATION_MIGRATIONS`)
  - `services/localization/tests/support/scratch_db.py`
  - `services/localization/tests/agents/conftest.py` (`glossary_db` phải áp chuỗi có `collections`)
  - `services/localization/tests/test_migration.py`
- Modify: `services/downloader/test/db/schema-migration.spec.ts` (`EXPECTED_TABLES`)
- Modify: `services/localization/tests/starter/test_grants.py`
- Create: helper fixture dùng chung tạo collection kênh; sửa mọi fixture insert vào `videos`
  (khoảng 31 file, liệt kê bằng `rg -l "INSERT INTO videos|insert\(videos\)"` lúc làm).

## Implementation Steps

1. Test trước:
   - `schema-migration.spec.ts` thấy bảng và cột mới.
   - Backfill (DB có hai kênh, video, glossary cũ): mỗi kênh đúng một collection `channel`, mọi video
     có `collection_id`, glossary đổi khóa đúng, số dòng không đổi.
   - Down trên DB có collection `mix` và `manual` với thuật ngữ trùng: chạy được, giữ đúng dòng của
     collection kênh, báo đúng số dòng bỏ.
   - `test_grants.py` với `SET ROLE localization`: đọc được `videos.collection_id`, `mix_episode`,
     `collections`; claim và settle được `folder_exports`; không sửa được `target_subdir`.
   - `apply-migrations.test.sh` xanh, kể cả nhánh không có ledger.
2. Migration downloader: tạo bảng; tạo collection kênh từ `profiles` với tên
   `coalesce(nullif(btrim(left(nickname,200)),''), sec_user_id)`; gán `videos.collection_id`; đặt NOT NULL.
3. Migration glossary như mục Data Model.
4. Cập nhật `schema.ts`, các danh sách viết cứng, helper fixture.

## Todo

- [x] Test schema, backfill, down, grants
- [x] Migration downloader và down
- [x] Migration glossary và down
- [x] `schema.ts`, các danh sách migration, `migration-evidence.sql`, helper fixture

## Success Criteria

- `pnpm --filter @reup-flow/downloader test` xanh với Postgres test.
- `cd services/localization && CI=1 uv run pytest tests/test_migration.py tests/starter/test_grants.py tests/agents` xanh.
- `scripts/tests/apply-migrations.test.sh` xanh.

## Ghi chú khi làm (2026-11-14)

- Cầu nối tối thiểu để tiêu chí của phase này xanh: `upsertMany` tạo collection kênh (idempotent) và gán
  `collection_id` của kênh cho video mới; `GlossaryStore` đổi `channel_id` của lượt chạy thành id
  collection kênh trong SQL. Phase 3 thay bằng `resolveCollections`, phase 5 thay bằng `spec.collection.id`.
- Thêm so với Data Model, theo review: CHECK `collections_owner_check` (collection kênh có
  `sec_user_id = external_id`, 合集 luôn có `sec_user_id`); `save_subdir`/`target_subdir` từ chối ký tự
  điều khiển; `file_stem` phải là `{aweme_id}` hoặc `E{nn}-{aweme_id}` của chính video đó; unique index một
  phần `folder_exports_one_open_idx (aweme_id, generation)` cho dòng `pending`/`exporting` (phase 4 dùng
  `ON CONFLICT DO NOTHING`); index claim theo `id`.
- Bỏ grant `REFERENCES` trên `collections`: FK của glossary do admin tạo, kiểm tra FK chạy bằng chủ bảng;
  `localization` chỉ cần `SELECT` (có test chứng minh ghi glossary được và không tạo được FK riêng).
- Phase 6: câu claim cần `FOR UPDATE SKIP LOCKED` trong subquery (như `final_render.py`), nếu không hai
  worker có thể cùng claim một dòng. Test grant đã dùng dạng này.
- Phase 8: DB compose (5433) chưa có ledger `applied_migrations`; lần chạy thật sẽ nhận 6 migration cũ và áp
  6 migration (`261108`, hai `261112`, `261113`, hai `261114`). Nếu migration glossary dừng vì dòng không
  khớp sau khi `261114-collections` đã commit, phải xử lý dòng glossary rồi chạy lại trước khi bật service.

## Risk Assessment

- Down làm mất collection, setting và thuật ngữ của collection không phải kênh: ghi trong file down;
  backup trước khi áp.

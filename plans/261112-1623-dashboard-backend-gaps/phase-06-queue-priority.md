---
phase: 6
title: "Ưu tiên hàng đợi localization"
status: pending
priority: P2
effort: "5h"
dependencies: [5]
---

# Phase 6: Ưu tiên hàng đợi localization

## Goal

Admin đánh dấu "Ưu tiên" khi đưa video vào localization để video đó được starter lấy trước các video đang chờ (hạng mục 8, quyết định D3 phương án a: chỉ hàng đợi localization).

## Context

- Starter lấy dòng outbox bằng `ORDER BY next_attempt_at ... LIMIT 1 FOR UPDATE SKIP LOCKED` (`services/localization/src/localization/starter/outbox_starter.py:61-69`) và các run của hai pipeline cùng đếm vào một giới hạn đồng thời (`outbox_starter.py:237`). Nghẽn nằm ở giới hạn này, nên ưu tiên chỉ đổi **thứ tự lấy**, không vượt giới hạn và không chen run đang chạy.
- **Starter chạy bằng role `localization` (NOSUPERUSER)** với `LOC_DATABASE_URL`, và role này chỉ có `SELECT (aweme_id, generation, state, next_attempt_at, dispatch_attempts) ON localization_outbox` (`services/localization/migrations/261026-outbox-starter-grants.sql:16-17`). Dùng cột `priority` trong `ORDER BY` cần `SELECT` trên cột đó; thiếu grant thì **mọi** truy vấn claim lỗi `permission denied`, `run_forever` nuốt lỗi và vòng lặp tiếp (`outbox_starter.py` xử lý `psycopg.Error`), không video nào được gửi và dashboard vẫn báo Postgres bình thường. Không có role Postgres nào tên `downloader`: downloader chạy bằng superuser `reup` (`infra/postgres/create-databases.sh:45-47`), nên test bằng superuser không bắt được lỗi này.
- File grant cũ (`261026-outbox-starter-grants.sql`) đã nằm trong sổ `applied_migrations`, nên `apply-migrations.sh` sẽ **bỏ qua** nếu chỉ sửa file đó (`scripts/apply-migrations.sh:57-63`); phải là migration mới với tên riêng. `scripts/migration-evidence.sql` nhận một migration là "đã áp" nếu schema của nó đã có mà không chạy lại, nên migration grant phải trả `false` ở nhánh adopt như mục grant hiện có.
- Với link đơn, **dòng outbox chưa tồn tại lúc bấm**: `POST /api/videos` chỉ đặt `videos.localize_on_done` (`videos.service.ts:60-69`), `localize()` trả `no_outbox_row` (`intake.repository.ts:54`), và dòng outbox sinh sau khi tải xong ở `markDone` (`videos.repository.ts:266-278`) thẳng ở trạng thái `pending` hoặc `held` từ `localize_on_done`. Vì vậy cờ ưu tiên phải được lưu trên `videos` (cùng chỗ với `localize_on_done`) rồi `markDone` chép sang outbox.
- `rerun` (`dashboard.actions.ts:200-205`) UPDATE đặt `generation`, `state`, `attempts`… nhưng không đụng `priority`: một video từng ưu tiên sẽ giữ ưu tiên ở mọi lần chạy lại nếu không đặt tường minh.
- Mutation `services/downloader/test/mutations/phase-12.sh` và `scripts/backfill-localization-outbox.sql` chèn vào outbox; cột mới phải có `DEFAULT` để chúng không hỏng.

## Thiết kế

- **Cột ưu tiên:** `localization_outbox.priority smallint NOT NULL DEFAULT 0 CHECK (priority IN (0, 1))` và `videos.localize_priority boolean NOT NULL DEFAULT false` (migration phía downloader).
- **Grant (migration riêng, luôn được áp):** `GRANT SELECT (priority) ON localization_outbox TO localization`, theo mẫu `DO $$ ... pg_roles` của `261112-dashboard-intake.sql`, đặt ở `services/localization/migrations/` với tên mới; `.down.sql` `REVOKE` trước khi xóa cột; mục tương ứng trong `migration-evidence.sql` trả `false`.
- **Starter:** `ORDER BY priority DESC, next_attempt_at`; chỉ đổi thứ tự, giữ `SKIP LOCKED`, vòng retry, backoff và giới hạn đồng thời.
- **Downloader:**
  - `POST /api/videos` với `localize: true` nhận `priority?: boolean` và lưu vào `videos.localize_priority`.
  - `markDone` chép `localize_priority` vào `priority` của dòng outbox mới trong cùng transaction.
  - Hành động `localize` (video đang `held`) nhận cờ và đặt `priority` khi chuyển sang `pending`.
  - `rerun` luôn ghi `priority = $cờ ?? 0` tường minh (mặc định 0, ưu tiên không tự động giữ lại; ưu tiên chỉ áp khi admin chọn). <!-- Updated: Validation Session 1 - đã xác nhận đặt lại về 0 khi chạy lại -->
  - Cờ ưu tiên ở dòng đã `pending` không đổi được từ dashboard trong phase này (không có "nâng ưu tiên" cho dòng đang chờ), để phạm vi gọn.
- **Form tải:** ô "Ưu tiên" chỉ hiện cho đường "Localize ngay" của link video đơn; với job kênh ẩn (video chưa tồn tại lúc gửi job).
- **UI:** công tắc "Ưu tiên" trong hộp xác nhận của Đưa vào localization và Chạy lại; dòng bảng hiện nhãn "Ưu tiên" khi `priority = 1` ở giai đoạn Chờ chạy; chú thích "đứng trước hàng chờ, không chen run đang chạy".
- **Thứ tự triển khai:** chạy cả hai migration (cột và grant) trước, rồi mới build lại `localization-starter`. Rollback: hoàn tác starter trước, sau đó chạy `.down.sql`. Mã dashboard (`nest`) cũng chỉ build sau khi cột có mặt.

## Files

- Create: `services/downloader/migrations/<ngày>-outbox-priority.sql` và `.down.sql` (cột outbox và `videos.localize_priority`), `services/localization/migrations/<ngày>-outbox-priority-grant.sql` và `.down.sql`; đăng ký theo bảng ở `plan.md` mục "Đăng ký migration"; Modify `services/downloader/src/db/schema.ts`
- Modify: `services/localization/src/localization/starter/outbox_starter.py` (ORDER BY)
- Modify: `services/downloader/src/videos/videos.service.ts`, `src/videos/videos.schemas.ts`, `src/db/videos.repository.ts` (`markDone`), `src/dashboard/intake.repository.ts`, `dashboard.actions.ts`, `dashboard.schemas.ts`, `dashboard.repository.ts` (trả `priority` trong danh sách, cùng cách như trường khác)
- Modify: `services/web/src/components/JobForm.tsx`, hộp xác nhận hành động (`ConfirmDialog.tsx` hoặc nơi gọi), `PipelineTable.tsx`, `services/web/src/api/types.ts`, `client.ts`
- Tests: `services/localization/tests/starter/` (chạy claim bằng role `localization` thật; `tests/support/scratch_db.py` phải nạp migration mới), `services/downloader/test/dashboard/`, `scripts/tests/apply-migrations.test.sh`, `services/downloader/test/mutations/phase-12.sh` nếu cần

## Tasks

1. Sao lưu DB, thêm hai migration (cột và grant), kiểm down/up, kiểm `migration-evidence.sql` không bỏ qua grant trên DB đã có schema.
2. Sửa `ORDER BY` trong starter; test chạy bằng role `localization` để chứng minh claim không còn `permission denied`; test thứ tự (ưu tiên trước dù `next_attempt_at` muộn hơn; bằng ưu tiên theo `next_attempt_at`; không vượt giới hạn đồng thời).
3. `POST /api/videos`, `markDone`, `localize`, `rerun` xử lý cờ theo thiết kế; ghi `dashboard_actions`.
4. Test đầu-cuối: enqueue link đơn có ưu tiên → `markDone` → dòng outbox `priority = 1`; rerun không ưu tiên đặt lại `priority = 0`.
5. UI: ô trong form tải, công tắc trong hộp xác nhận, nhãn trên dòng, chú thích.
6. Chạy lại mutation `phase-12` và `scripts/backfill-localization-outbox.sql` trên DB tạm để chắc cột mới không làm hỏng.

## Verification

- Test starter (role `localization`), downloader và web xanh; migration test xanh; mutation phase-12 vẫn bắt được lỗi như trước.
- Trên stack thật: hai video `pending`, một có ưu tiên; khi có chỗ trống, starter lấy video ưu tiên trước (kiểm bằng log hoặc `workflow_id` được gán trước); starter vẫn dispatch bình thường sau khi build lại.

## Risks

- Đổi thứ tự lấy chạm vào starter của pipeline dubbing: chỉ đổi `ORDER BY`, giữ nguyên phần còn lại; test hồi quy cho cả hai pipeline.
- Sai thứ tự triển khai làm starter ngừng: trình tự migration → build starter và trình tự rollback được ghi trong phase 8 và kiểm bằng test.
- Ưu tiên đói video thường nếu admin đánh dấu quá nhiều: chấp nhận ở bản một người dùng.
- Cờ ưu tiên cho job kênh không được hỗ trợ (hạn chế đã nêu).

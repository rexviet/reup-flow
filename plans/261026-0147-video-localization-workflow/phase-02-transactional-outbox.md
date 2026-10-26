---
phase: 2
title: "Outbox cùng transaction với markDone"
status: pending
priority: P1
effort: "0.5d"
dependencies: [0]
---

# Phase 02: Outbox cùng transaction với markDone

<!-- Updated: Red Team 2026-10-26 - bảng không kèm schema để giữ test cô lập, thêm generation và state, down không CASCADE, thứ tự rollback -->

## Goal

Bất biến cần giữ: **video đã `done` thì luôn có dòng trong `localization_outbox`**. Downloader
vẫn không biết gì về Temporal (D1, D2), và test cô lập theo `search_path` vẫn nguyên vẹn.

## Files to Create / Modify

- Create: `services/downloader/migrations/261026-localization-outbox.sql` và `.down.sql`. Tên bảng **không kèm schema**.
- Modify: `services/downloader/src/db/schema.ts`. Thêm bảng `localizationOutbox`, **không dùng `pgSchema`**.
- Modify: `services/downloader/src/db/videos.repository.ts`. `markDone` ghi outbox trong cùng transaction.
- Modify: `services/downloader/src/lanes/download.lane.ts:456`. Gọi `markDone` trong `db.transaction`.
- Modify: ESLint config. Thêm `no-restricted-imports` cho `@temporalio/*`.
- Modify: `services/downloader/test/support/db.ts`. Apply migration mới vào schema riêng của mỗi lần chạy.
- Create: `scripts/backfill-localization-outbox.sql`
- Create: `services/downloader/test/db/outbox.integration-spec.ts`
- Create: `services/downloader/test/mutations/phase-12.sh`
- Modify: `scripts/run-mutations.sh`. Thêm dòng `12:1:0` vào `EXPECT`.

## Tasks & Steps

1. **Migration.** Theo convention: tên bảng không kèm schema, không dùng `CASCADE`.

   ```sql
   CREATE TABLE localization_outbox (
     aweme_id          text PRIMARY KEY REFERENCES videos(aweme_id),
     generation        integer NOT NULL DEFAULT 1,
     state             text NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending','dispatched','rejected','dead')),
     enqueued_at       timestamptz NOT NULL DEFAULT now(),
     next_attempt_at   timestamptz NOT NULL DEFAULT now(),
     dispatched_at     timestamptz,
     workflow_id       text,
     dispatch_attempts integer NOT NULL DEFAULT 0,
     last_error        text
   );
   CREATE INDEX localization_outbox_due_idx ON localization_outbox (next_attempt_at) WHERE state = 'pending';
   ```

   - `.down.sql` chỉ có `DROP TABLE localization_outbox;`.
   - Grant cho role `localization`: `SELECT, UPDATE` trên `localization_outbox`, `SELECT` trên `videos`.
     Grant chạy bằng script ops, không nằm trong migration, để test schema không cần role đó.
2. **`markDone`.**
   - `UPDATE videos` và `INSERT INTO localization_outbox (aweme_id) VALUES ($1) ON CONFLICT DO NOTHING` chạy trong **một transaction**.
   - Thêm comment giải thích vì sao bắt buộc phải cùng transaction.
3. **Test drift.** Mở rộng test drift giữa schema và migration để phủ bảng mới. Bảng mới nằm cùng
   schema với `videos`, nên filter `table_schema` hiện có vẫn đúng.
4. **Chạy lại một video.**

   ```sql
   UPDATE localization_outbox
      SET generation = generation + 1, state = 'pending', dispatched_at = NULL,
          next_attempt_at = now(), dispatch_attempts = 0
    WHERE aweme_id = $1;
   ```

   Lệnh này được ghi vào runbook ở phase 10.
5. **Backfill.**
   - **Backup DB trước**, và chỉ chạy **sau khi phase 01 đã migrate xong**.
   - Chỉ chèn những video có `storage_path LIKE 's3://%'`.
6. **Thứ tự rollback**, ghi trong phần Rollback: deploy code downloader không còn ghi outbox
   **trước**, rồi mới chạy `.down.sql`. Làm ngược lại thì mọi lần `markDone` sẽ fail và đốt hết
   lượt `attempts`.

## Verification

Chạy `pnpm --filter @reup-flow/downloader test:integration -- outbox`:

| Test | Kỳ vọng |
|---|---|
| `markDone` | Video `done` và có đúng một dòng outbox. Dòng đó nằm **trong schema test**, không phải `public` |
| Gọi `markDone` hai lần | Vẫn một dòng |
| Inject lỗi vào INSERT | Video vẫn `downloading` |
| Hai suite chạy chồng nhau | Không thấy dòng outbox của nhau |

Các bước khác:
- Toàn bộ suite integration của downloader vẫn xanh. Đây là bằng chứng test cô lập không bị vỡ.
- `bash scripts/run-mutations.sh 12`: mutation đưa INSERT ra ngoài transaction làm test đỏ.
- `pnpm --filter @reup-flow/downloader lint`: thêm `import '@temporalio/client'` thì lint báo lỗi.

## Risk

Khoá ngoại tới `videos` làm downloader không xoá được video đã có outbox. Hiện chưa có đường nào xoá video.

## Rollback

1. Deploy code downloader không ghi outbox.
2. Chạy `.down.sql`.
3. Có backup DB trước khi làm.

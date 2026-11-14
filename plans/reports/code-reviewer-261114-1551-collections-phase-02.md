# Code Review: Collections phase 2 (schema and backfill)

## Scope
- Branch `feat/localization-dubber-code-mode`, uncommitted working tree.
- New: `services/downloader/migrations/261114-collections{,.down}.sql`,
  `services/localization/migrations/261114-glossary-collection-scope{,.down}.sql`,
  `services/downloader/test/support/collections.ts`, `services/downloader/test/db/collections-migration.spec.ts`.
- Modified: `schema.ts`, `videos.repository.ts`, `activities/glossary.py`, `scripts/apply-migrations.sh`,
  `scripts/migration-evidence.sql`, `scripts/tests/apply-migrations.test.sh`, about 31 downloader fixtures, 5 Python test files.
- About 720 LOC added in tracked files, plus about 550 LOC in new files.
- Checked against the phase 2 spec, the decisions in plan.md, and what phases 3–6 expect.

## Verification performed
| Check | Result |
|---|---|
| `uv run pytest tests/test_migration.py tests/starter/test_grants.py tests/agents` | 367 passed, **1 failed**: `tests/agents/test_localizer.py::test_deps_carry_keys_and_numbers_never_video_content`. The cause is `source_video_key`, added in committed `d83aecd`. This change did not cause it, but the phase criterion "tests/agents green" is still not met. |
| Downloader `npx jest -i` (full suite, in band) | 1242 passed, 23 failed in 8 suites. All 8 fail for environment reasons: hard-coded `localhost:5432` (db.module, lanes.module, health, probe-health, bootstrap, spike-skip-locked, cookie-leak) and compose-constraints. None of the 8 is touched by this diff. Running without `-i` gives "too many clients". |
| `tsc --noEmit`, `eslint` on the touched files | clean |
| `scripts/tests/apply-migrations.test.sh` (disposable `reup_migration_test_*` DB) | PASS, including the down/up round trip and the ledger-less adoption branch. The run leaves dump files in the gitignored `backups/`. |
| Transaction-control awk guard on all 4 new files | all accepted. The PL/pgSQL `BEGIN` sits on the `DECLARE …; BEGIN` line, so no line starts with BEGIN. |
| Read-only preflight on the real DB (5433, `public`, inside a rolled-back tx with a pg_temp function) | 0 orphan videos, 0 glossary rows without a profile, 3 profiles (none has a nickname), 4 glossary rows, **no `applied_migrations` ledger**. Evidence dry run: init/outbox/core/llm-cost/llm-provider are adopted; `261108`, `261112×2`, `261113`, `261114×2` are absent and would be applied. Neither migration's RAISE path would fire today. |
| Is the REFERENCES grant needed? (scratch DB, grant removed) | No. As `localization`, an insert into `localization.glossary` still passes the FK check. |

## Overall assessment
The migrations are correct on the paths that matter. The backfill, the RAISE-and-abort paths, the PK swap, the glossary down migration (ranking, dedupe, NOTICE), the ordering (glossary down before collections down; no CASCADE) and the ledger-less evidence all behave as the spec says, and the tests prove it. The red paths are real: removing either RAISE, the `kind='channel'` ranking or the `sec_user_id IS NULL` drop clause turns at least one test red. The drizzle schema matches the SQL for columns, types, nullability, indexes, the unique constraint and all 5 FKs. I checked the FKs by hand because the drift test does not compare FKs.

I found no Critical or High issues. Two Medium items are schema and test gaps that phases 4 and 6 will hit.

## Critical
None.

## High
None.

## Medium

### M1. `folder_exports` has no uniqueness for open requests, so phase 4's "click twice, one pending row" cannot be made race-free
- `services/downloader/migrations/261114-collections.sql:80-103`
- **Scenario:** Phase 4 requires no new row when a `pending` or `exporting` row already exists for the same `(aweme_id, generation)`. The dashboard sends two POSTs (a double click, or a retry after a timeout). Both read "no open row" and both insert. The Mac worker then exports the same video twice, two rows race on the same target file, and both report `done`.
- **Why phase 2:** this phase owns the schema. Without the index, phase 4 must either take an explicit lock or add another migration. A check-then-insert in autocommit is not safe.
- **Fix:** add `CREATE UNIQUE INDEX folder_exports_one_open_idx ON folder_exports (aweme_id, generation) WHERE state IN ('pending','exporting');` with the matching `uniqueIndex(...).where(...)` in `schema.ts`. Add it to `migration-evidence.sql`, and add a test showing the second insert conflicts. Phase 4 can then use `INSERT … ON CONFLICT DO NOTHING`.

### M2. The claim statement in the new grants test (copied from phase 6) can hand one export to two workers
- `services/localization/tests/starter/test_grants.py:112-119`. The source is `phase-06-mac-folder-export-worker.md:19-20` and `:32`.
- **Scenario:** In `UPDATE … WHERE id = (SELECT id … WHERE state='pending' … LIMIT 1)`, the subquery is an InitPlan that runs once. Under READ COMMITTED, worker B blocks on A's row lock. When A commits, B re-checks only the outer `id = $0`, which is still true, so B also claims the row and overwrites `claim`. Both workers download, and A's settle then fails the `claim` match. Phase 6 explicitly requires that "two workers claiming at once: only one processes".
- **The premise is wrong:** phase 6 says autocommit rules out `FOR UPDATE SKIP LOCKED`. But `final_render.py:26-43` already uses `FOR UPDATE OF c SKIP LOCKED` inside the subquery of a single autocommit statement, and that works because the lock only needs to last for that one statement. The column-level `UPDATE` grant on `folder_exports` is enough for `SELECT … FOR UPDATE`.
- **Fix:** in the test (and in phase 6), add `FOR UPDATE SKIP LOCKED` to the subquery, and/or repeat the state/lease predicate in the outer WHERE. Update the Key Insight in phase 6. No schema change is needed.

## Low

### L1. The REFERENCES grant on `collections` is unnecessary, and its comment is wrong
- `services/downloader/migrations/261114-collections.sql:108-109`
- The FK check runs as the owner of the referenced table (`reup`), and the FK is added by the superuser. I removed the grant in a scratch DB and `localization` could still insert terms. With the grant, `localization` (which owns schema `localization` and can create tables there) can add its own FKs to `collections`, which can block deletes or key updates on it.
- **Fix:** grant `SELECT` only and drop the comment. `test_glossary_rows_are_writable_by_the_worker_role_after_the_move` already shows the worker can write without it.

### L2. Nothing enforces that a channel collection's owner is its external id, but the down migration depends on it
- `services/downloader/migrations/261114-collections.sql:19-43`; `261114-glossary-collection-scope.down.sql:21,29`
- The up migration and `glossary.py` look up a channel by `external_id`. The down migration groups and rewrites by `sec_user_id`. If phase 3 or 4 ever creates a `channel` row with a different or NULL `sec_user_id`, a rollback silently moves or drops that channel's terms. A `mix` row with NULL `sec_user_id` also has every term dropped on rollback. The NOTICE counts them, but nothing stops it.
- **Fix:** `CHECK (kind <> 'channel' OR sec_user_id = external_id)`, and probably `CHECK (kind <> 'mix' OR sec_user_id IS NOT NULL)`, since phase 3 always knows the channel.

### L3. Partial-upgrade state if the glossary precheck fails
- `scripts/apply-migrations.sh` runs each migration in its own transaction. The unmatched-glossary RAISE lives only in `261114-glossary-collection-scope.sql:19-28`.
- **Scenario:** `261114-collections` commits, with its ledger row, and then the glossary migration aborts. At that point the old downloader cannot insert videos (`collection_id` is NOT NULL), and the new `glossary.py` cannot query (`collection_id` does not exist yet). The window is maintenance-only and the real DB has 0 unmatched rows today, so the practical risk is small.
- **Fix (optional):** repeat the same count at the top of the collections migration, guarded by `to_regclass('localization.glossary') IS NOT NULL`, so the upgrade fails before any change. Or note this recovery step in the phase 8 runbook.

### L4. Some assertions would pass for the wrong reason
- `collections-migration.spec.ts:170`, `:266`: `.resolves.toBeTruthy()` accepts any error, including a typo in the fixture SQL. Assert the constraint name or the error type, as the `check constraint` cases at `:189` already do.
- `test_migration.py:328`: `"3" in n` also matches "13" or "30". Use an exact match: `"dropped 3 rows" in n`.
- `apply-migrations.test.sh` does not test the path production will actually take: no ledger, at the pre-collections state. Its ledger-less branch runs after everything is applied. My read-only dry run on the real DB shows the evidence resolves correctly. Consider adding a test case: apply up to `261113`, drop the ledger, run.

### L5. Interim GlossaryStore: a channel without a channel collection fails the activity
- `services/localization/src/localization/activities/glossary.py:36-47`
- `UPSERT_SQL` raises `NotNullViolation`, and nothing catches it in `localizer_ports.py:72` or the agent tool, so Temporal retries the activity and then fails it. Today `upsertMany` always creates the channel collection, so this cannot happen. Phase 3's `resolveCollections` may stop creating one for a channel whose videos all go to 合集. Phase 3 and phase 5 must ship in the same window (plan.md already requires that). When phase 5 rewrites this, decide whether a missing collection means "no glossary write" with a message to the agent, or a hard failure.

### L6. Value checks left to later phases
- `save_subdir` and `target_subdir` (`:35-37`, `:85-86`) accept control characters such as `\n` and segments that are only spaces. The Mac worker trusts what is in the DB. Add `AND save_subdir !~ '[[:cntrl:]]'`, matching phase 4's no-NUL intent.
- `file_stem` (`:88`) does not have to end with `aweme_id`. It could be `right(file_stem, char_length(aweme_id)) = aweme_id`. This is defense in depth, because the downloader writes the row.
- `folder_exports_open_idx` is on `requested_at`, but phase 6 claims `ORDER BY id`. With small tables this is harmless; index `(id)`, or order the claim by `requested_at`.

## Judgement on the two deliberate deviations
1. **The minimal changes to `upsertMany` and `GlossaryStore` are accepted.** Without them, the NOT NULL `collection_id` and the column rename would make this phase's own success criteria impossible. The changes are small:
   - an idempotent `INSERT … SELECT FROM profiles ON CONFLICT DO NOTHING`, plus a scalar subquery;
   - parameters bound through drizzle `sql`;
   - the subquery cannot return two rows, because of `UNIQUE(kind, external_id)`;
   - under READ COMMITTED the next statement sees a collection another transaction committed.

   Tests cover new, missing and repeated channels, and the no-profile case. Phases 3 and 5 own the replacement logic.
2. **The stricter CHECKs are accepted.** Rejecting empty segments matches phase 4's zod rule ("each segment is not empty, `.` or `..`") and rejects nothing phase 4 would accept. The `file_stem` CHECK is stricter than phase 6's Python regex, which is good: Python's `$` matches before a trailing `\n`, and `\d` matches non-ASCII digits. Postgres `[0-9]` and `$` do neither, so the DB stops what a sloppy `re.match` would let through.

## Positive observations (for risk calibration)
- Both RAISE paths abort the whole file's transaction. The tests prove that nothing was created and the rows are unchanged.
- The `localization` grants are column-scoped, and `target_subdir`, `file_stem`, `final_key`, `aweme_id`, insert and delete are denied as tested.
- The core migration's evidence entry accepts either glossary key, and the ledger-less branch exercises it.

## Recommended actions
1. M1: add the partial unique index for open exports, plus schema.ts, the evidence entry and a test.
2. M2: fix the claim SQL in the test and the phase 6 Key Insight (SKIP LOCKED in the subquery, or a repeated outer predicate).
3. L1, L2: drop REFERENCES; add the `channel` and `mix` owner CHECKs.
4. L4: tighten the three weak assertions; consider a test for the ledger-less pre-collections path.
5. Separately: fix the `test_localizer.py` failure that predates this change (from `d83aecd`), or record it, before claiming "tests/agents green".

## Plan follow-ups (do not edit the plan here)
- The phase 2 todo items look complete apart from the follow-ups above. One success criterion still fails because of the earlier `test_localizer` failure.
- Phase 6's Key Insight about autocommit and SKIP LOCKED should be corrected (M2).
- Phase 8 runbook: the real DB (5433) has no ledger, so the first `apply-migrations.sh` run adopts 6 migrations and applies 6, including `261108`, `261112×2` and `261113`, not only the two new ones. Confirm that this DB is the intended target and expect that set.

## Unresolved questions
- Is the compose DB on 5433 the production target? `261112-dashboard-intake` is absent there, yet the dashboard is in use. The production DB may be elsewhere, or this DB has never been through `apply-migrations.sh`.
- Should a `mix` collection always carry `sec_user_id`? This decides whether L2's second CHECK is right.

## Xử lý (implementer, 2026-11-14)

Mỗi mục sửa đều có test đỏ trước (9 test jest và 1 test pytest đỏ), rồi mới sửa migration.

| Mục | Xử lý |
|---|---|
| M1 | Đã sửa: thêm `folder_exports_one_open_idx` (unique một phần trên `(aweme_id, generation)`, chỉ cho `pending` và `exporting`), cùng `uniqueIndex` trong `schema.ts`, mục evidence và test. |
| M2 | Đã sửa câu claim trong test: thêm `FOR UPDATE SKIP LOCKED`. Key Insight của phase 6 để phase 6 tự sửa; đã ghi lại trong ghi chú của phase 2. |
| L1 | Đã bỏ grant `REFERENCES`, chỉ còn `SELECT`. Có test cho thấy role `localization` không tạo được FK riêng tới `collections`. |
| L2 | Đã thêm `collections_owner_check`: collection kênh phải có `sec_user_id IS NOT DISTINCT FROM external_id`, và 合集 phải có `sec_user_id`. |
| L3 | Chưa sửa. Đặt cùng phép kiểm vào migration downloader thì phải tra `localization.glossary` toàn cục. Như vậy migration sẽ hỏng trong các schema test của downloader trên DB `reup` (glossary thật nằm ở đó). Đã ghi bước khôi phục vào ghi chú cho phase 8. |
| L4 | Đã siết: các trường hợp từ chối giờ khớp đúng tên constraint, và NOTICE phải khớp `dropped 3 rows`. Chưa thêm test cho đường "chưa có ledger, đang ở trạng thái trước collections"; reviewer đã chạy thử khô trên DB thật. |
| L5 | Ghi nhận cho phase 3 và phase 5. |
| L6 | Đã sửa: `save_subdir` và `target_subdir` từ chối ký tự điều khiển; `file_stem` phải khớp `aweme_id` của chính video đó; index claim chuyển sang `(id)`. |

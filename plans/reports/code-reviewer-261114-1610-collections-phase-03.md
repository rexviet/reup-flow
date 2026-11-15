# Code Review: Collections phase 3 (assign collection at intake)

## Scope
- Change under review: uncommitted working tree on `feat/localization-dubber-code-mode`, checked against `plans/261114-0442-collections/phase-03-assign-collection-at-intake.md` and the locked decisions in `plan.md`.
- Source: new `src/db/collections.repository.ts`; `src/db/videos.repository.ts`, `src/db/jobs.repository.ts`, `src/lanes/enumerate.lane.ts`, `src/videos/{videos.service,videos.schemas,video-enqueue.controller}.ts`, `src/jobs/{jobs.service,jobs.schemas,jobs.controller}.ts`.
- Tests: new `test/db/collections.repository.spec.ts` and `test/lanes/enumerate-collections.spec.ts`; changes to `test/db/videos.repository.spec.ts`, two e2e specs, and `test/support/aweme-fixtures.ts`.
- Size: about 330 lines of diff plus about 290 lines in the new repository file.

## Verification run
- `tsc --noEmit` (src): clean. `eslint` on the changed src and test files: clean.
- The 5 touched or new suites, run against isolated test schemas on 5433: 5/5 suites passed, 108/108 tests passed. I applied no migrations and touched no data in `public`.
- I reviewed the author's claim of 28/28 mutations killed and the full-suite baseline (89 suites, the same 23 failures as HEAD). I did not rerun them.

## Overall assessment
The change is correct and close to the spec. SQL types are right:
- `VALUES` columns resolve to text. `::uuid` and `::integer` are cast explicitly where a type has to be inferred.
- The `null::integer` episode works.

Owner and FK constraints hold on every path:
- A channel row takes `sec_user_id` and `external_id` from the same `profiles` row.
- A mix row always has an owner.
- The owner's profile exists before any insert. In enumerate it is the job's profile. In the single-video path, `upsertProfile` runs first in the same transaction.

The concurrency test proves the two writers really overlap, and its setup makes the mix INSERT the only possible wait point. I found no Critical or High defects. The Medium findings are two spec deviations in the manual-pick behaviour, which need a user decision rather than more code.

### Deliberate choices, evaluated
| Choice | Verdict |
|---|---|
| Manual pick applies to new videos only | Defensible for jobs. For the single-video endpoint it fails silently (M1). |
| Manual pick suppresses moving existing videos into 合集 | Deviates from the spec text (M2). |
| A manually picked video still records `mix_episode` | Fine as data. Phase 4's file-stem rule must take it into account (L4). |
| Channel collection is always created | Correct. It closes prior review L5. It runs even for a manual pick (test `still creates the channel collection ...`). |
| `upsertMany(VideoUpsert & CollectionPlacement)` | Good. The compiler forces every caller to pass a placement. `src/` has only one caller. |
| No transaction around resolve/upsert/move in enumerate | Safe. Every statement is idempotent and autocommits, so enumerate holds each lock only for one statement. A crash between steps leaves an empty collection or an unmoved video, and the next run repairs it. |
| Mix ids inserted in sorted order | Correct reasoning: lock order follows `VALUES` order, and both writers sort in JS the same way. It is untested and only defensive, because F2 allows one enumerate at a time and the single-video path inserts at most one mix (L2). |
| `INSERT ... ON CONFLICT DO NOTHING` then `SELECT` | Correct under READ COMMITTED: each statement takes a new snapshot, so the SELECT sees the other writer's committed row. The concurrency test proves it. |
| `coalesce(m.episode, v.mix_episode)` | Correct. The MOVE branch keeps an existing episode when Douyin sends none. The FILL branch only fires when `v.mix_episode IS NULL`. |

### Deadlock analysis (single-video transaction vs enumerate)
- The single-video transaction T locks the profile, then the channel collection, then at most one mix collection, then its one video. Enumerate's statements autocommit.
- A deadlock would need one enumerate statement to hold lock X while waiting for Y, while T holds Y and waits for X. T only ever touches its own profile, channel collection, mix and video. While an enumerate statement waits on one of these, it holds nothing else that T needs, so no cycle can form.
- Two single-video transactions for the same channel serialize at `upsertProfile`. Two for different channels sharing a mix: one waits on the mix insert, and the other never needs anything the first holds.
- A concurrent manual move by phase 4 (`PATCH` sets `collection_manual = true`) is safe. The guard in `moveIntoMixes` sits in the `WHERE` clause, and READ COMMITTED re-checks it against the newer row version, so the person's move wins.

## Critical
None.

## High
None.

## Medium

### M1. Single-video endpoint ignores `collection_id` for a video already stored, and still answers 202
- `src/db/collections.repository.ts:155-157` (`upsertMany` is `ON CONFLICT DO NOTHING`, and `moveIntoMixes` is skipped for a manual pick); `src/videos/videos.service.ts:80-94`.
- **Scenario:** A user pastes a video that is already downloaded and picks collection X in the form. The response is `202 {status: 'done'}`. The video stays in its channel collection or 合集, and nothing tells the user the pick was dropped. The phase spec says "Lựa chọn tay (form tải) đặt `collection_manual = true`" and does not limit this to new rows.
- For a channel job, dropping the pick for existing rows is defensible: re-filing a whole channel by accident is worse. For one video the user chose explicitly, it is a silent wrong answer.
- **Fix (needs a user decision):**
  - (a) In the single-video path, when a manual pick is given, also run `UPDATE videos SET collection_id = $pick, collection_manual = true WHERE aweme_id = $id`. This is an explicit user action, the same thing phase 4's `PATCH /videos/collection` will do.
  - (b) Or keep "new only", return something like `collection_applied: false` in the body, and show it in phase 7.
  - Either way, add an e2e test: a stored video posted again with `collection_id`.

### M2. A manual-pick job also skips moving untouched existing videos into their 合集 (MOVE) and filling missing episodes (FILL)
- `src/db/collections.repository.ts:157` (`if (input.manualCollectionId === null)`), and the early return at `:93-103`, which also skips `ensureMixCollections`.
- The spec's only exemption is for a video where "`collection_manual = true`", and `plan.md` says "trừ video đã chọn hoặc chuyển tay". Both are properties of the video, not of the job. Here the exemption is widened to every video touched by a job that has a pick.
- **Scenario:** After the migration, the operator reloads `IM-薛珍珠` and picks collection X for new uploads. The 10 backfilled episodes of `成名的代价` stay in the channel collection, so success criterion 1 (and phase 8's real run) is not met until a later reload without a pick.
- The test `is not moved into the collection a job picked by hand` (`test/db/collections.repository.spec.ts:546`) locks in this behaviour, and its title describes a different property (not moved into X) from what it asserts (not moved into the 合集 either).
- **Fix:**
  - (a) Run `ensureMixCollections` and `moveIntoMixes` regardless of the pick. New rows still go to the picked collection. `moveIntoMixes` already excludes `collection_manual` rows and touches only rows in the channel collection, so newly inserted manual rows are never moved.
  - (b) Or keep the behaviour and record it in `plan.md` as an accepted decision, with a test title that says what is asserted.

## Low

### L1. A collection deleted between the existence check and the insert gives 500, not 400
- `src/jobs/jobs.service.ts:46` checks outside the transaction. The foreign key on `jobs.collection_id` then raises 23503, which is not mapped and surfaces as 500.
- `src/videos/videos.service.ts:64,92`: the check inside the transaction (`resolveCollections`) takes no lock, so a delete after it surfaces as an FK violation on `videos.collection_id`, also a 500. The `CollectionNotFoundError` catch at `:92` only covers a delete in the narrow window before that check.
- Nothing deletes collections today. Phase 4 lists no DELETE, and the FKs block deleting a collection that has rows. So this is latent.
- **Fix:** when a delete endpoint arrives, map 23503 on these FKs to 400. Or read with `SELECT ... FOR KEY SHARE` inside the transaction, which blocks a concurrent delete until commit.

### L2. Untested branches and a weak assertion
- `src/videos/videos.service.ts:92`: no test reaches the `CollectionNotFoundError` to 400 mapping inside the transaction. Removing the catch keeps every test green.
- `src/db/collections.repository.ts`: no test can tell sorted from unsorted mix-id order. Acceptable, but the 28/28 mutation claim does not cover it.
- `test/db/videos.repository.spec.ts:659`: `rejects.toThrow()` passes for any error, for example a bad column. Assert the not-null violation, with code `23502` and column `collection_id`.

### L3. Mix ownership is first-seen-wins, including reposted or co-created items
- `src/db/collections.repository.ts:218-238` (`ensureMixCollections` owns the mix by `input.secUserId`).
- **Scenario:** The mapper notes that a channel's post list can contain items authored by another account. If such an item carries the other creator's `mix_info`, the 合集 collection is created with `sec_user_id` set to the reposting channel. When the real owner is enumerated later, its episodes go into a collection that phase 4 will list under the wrong "kênh sở hữu".
- **Fix:** when the item's `author.sec_uid` is known and differs from the channel, ignore `mix_info` and place the item in the channel collection. The mapper already runs this check for the nickname, so it could also null `mix`. Or accept the behaviour and note it.

### L4. `mix_episode` on manually filed videos is a contract that phase 4 must honour
- `src/db/collections.repository.ts:97-98`.
- Phase 4 builds `file_stem` as `E{tập:02d}-{aweme_id}` "hoặc `{aweme_id}`", and `plan.md` says "video ngoài 合集 là `{aweme_id}`".
- If phase 4 keys on `mix_episode IS NOT NULL` instead of `collections.kind = 'mix'`, a manual collection that mixes two 合集 will export `E01-a`, `E01-b`, `E02-a` and so on. File names still differ because they include the aweme id, but the ordering is misleading.
- **Fix:** state in phase 4 which signal decides the `E` prefix.

### L5. `collection_id: null` is rejected with 400
- `src/jobs/jobs.schemas.ts:63`, `src/videos/videos.schemas.ts:48`: `.optional()` rejects `null`.
- If the phase 7 form sends `null` for "no pick", every submission fails. Use `.nullish()` in the schema or make phase 7 omit the key, and say which in phase 7.

## Edge cases checked (no defect)
- Duplicate `aweme_id` within one page: `ON CONFLICT DO NOTHING` accepts duplicates, and `UPDATE ... FROM VALUES` updates the row once with no error.
- A row inserted in the same page then passes through `moveIntoMixes`: it matches neither branch, so nothing changes.
- Name length: `cleanDisplayName` cuts to 200 code points, which is what Postgres `char_length` counts, so the CHECK on name length holds. Fallback ids (`mixId`, `sec_user_id`) are shorter than 200.
- A nickname that is only the `sec_user_id` placeholder is not written to the profile (tested).
- A channel with no nickname gets the same name as the backfill would give it. Renaming happens only while the name equals the id, so a name a person set is kept (tested both ways for channel and mix).
- `claimOne` uses `.returning()` with no column list, so `job.collectionId` reaches the lane (e2e test plus the enumerate test through `runOnce`).
- The jobs reaper re-queues the same job row, so `collection_id` survives a reaped and reclaimed job.

## Positive observations (risk calibration)
- The concurrency test is real. It sets the profile nickname and channel name beforehand, so writer B's only possible wait is the mix insert, and it confirms `wait_event_type = 'Lock'` before A commits.
- The enumerate test drives `runOnce` with a real Douyin page, not a hand-built item.

## Recommended actions
1. M1 and M2: put both choices to the user (move-on-pick for a single video; whether a pick suppresses the MOVE and FILL into 合集), then either change the code or record the decision in `plan.md` and retitle the test.
2. L2: add the 400 test for the in-transaction path and tighten the not-null assertion.
3. L4 and L5: add one line each to phase 4 and phase 7.
4. L1 and L3: note them for phase 4 and phase 8.

## Plan follow-ups (do not edit the plan here)
- Phase 3 todo items look implemented: tests, repository and resolve, the move into 合集, and all three intake paths with both request schemas. The phase file still says `status: todo` and `plan.md` still lists the phase as Pending, so the lead should update them once M1 and M2 are decided.
- The "Related Code Files" entry for `upsertProfile` was not needed: the nickname write moved into `ensureChannelCollection`. This is fine; the plan text is just stale.

## Metrics
- Type check (src): 0 errors. Lint: 0 issues on changed files.
- Touched suites: 108/108 passing.

## Unresolved questions
- M1: should an explicit pick on a single, already-stored video move it?
- M2: should a job's pick stop backfilled videos from moving into their 合集?
- L3: should a 合集 seen on a reposted item belong to the reposting channel?

## Xử lý (implementer, 2026-11-14)

Mỗi mục sửa đều có test đỏ trước (4 test đỏ), rồi mới sửa code.

| Mục | Xử lý |
|---|---|
| M1 | Đã sửa theo câu spec "Lựa chọn tay (form tải) đặt `collection_manual = true`" (không giới hạn video mới cho đường video lẻ). Video lẻ đã có trong DB mà form gửi `collection_id` thì được chuyển vào collection đó và đánh dấu tay (`placeByHand`, chỉ chạy khi `inserted === 0`; video mới đã vào collection chọn tay ngay lúc insert). Gửi lại không có `collection_id` thì video đứng yên. Đây cũng là thao tác phase 4 làm khi chuyển collection, nên đảo lại được. Test: `moves a video already stored into the collection picked for it, marked manual`, `leaves a stored video where it is when no collection is picked`. Cần người dùng xác nhận (xem mục Concerns trong báo cáo cuối). |
| M2 | Đã sửa theo `plan.md` ("trừ video đã chọn hoặc chuyển tay"): job có collection chọn tay vẫn tạo 合集 và vẫn chuyển video cũ (không đánh dấu tay, đang ở collection kênh) vào 合集, kèm điền số tập. Collection chọn tay chỉ áp cho video mới. `resolveCollections` trả thêm `automatic` (chỗ của item nếu bỏ qua lựa chọn tay) để chuyển video cũ. Test: `still moves into its 合集 when the job picked a collection by hand`, `still moves backfilled episodes into the 合集 when the job picked a collection by hand` (trang thật IM-薛珍珠, 10 tập). Test cũ khẳng định "không tạo 合集" được đổi thành "合集 vẫn được tạo". |
| L1 | Chưa sửa: chưa có đường xóa collection. Phase 4 thêm xóa thì phải map lỗi FK `23503` ở `jobs.collection_id` thành 400. |
| L2 | Đã siết assertion NOT NULL: khớp `cause.code = '23502'` và `column = 'collection_id'`. Chưa có test cho nhánh `CollectionNotFoundError` → 400 trong transaction (chỉ xảy ra khi collection bị xóa giữa lúc kiểm và lúc ghi, mà chưa có đường xóa). Thứ tự chèn mix id được sắp chỉ để phòng thủ (enumerate chạy một instance). |
| L3 | Ghi nhận cho phase 8: 合集 thuộc kênh nhìn thấy nó đầu tiên. |
| L4 | Ghi nhận cho phase 4: video xếp tay vẫn giữ `mix_episode`; phase 4 phải chốt tiền tố `E01-` dựa vào `mix_episode` hay vào loại collection. |
| L5 | Ghi nhận cho phase 7: `collection_id: null` bị 400; dashboard phải bỏ hẳn khóa khi không chọn. |

Kiểm đột biến sau khi sửa: 34 đột biến khác nhau, 33 bị test hành vi giết (mọi đột biến chỉ gây lỗi biên dịch đều được chạy lại ở dạng hợp kiểu), 1 đột biến tương đương ("áp `placeByHand` cho cả video mới": video mới đã có đúng collection và cờ tay, nên không quan sát được khác biệt). Toàn bộ suite downloader: 89 suite, 1314 test, 23 lỗi đúng bằng tập lỗi có sẵn trên HEAD 9bde3f9.

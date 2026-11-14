# Code review: collections phase 7 (dashboard UI), uncommitted

## Scope
- Files: services/web/src/api/{client.ts,types.ts,client.test.ts}, components/{JobForm.tsx,JobForm.test.tsx},
  dashboard/{CollectionsPanel.tsx,CollectionGlossary.tsx (+tests),BulkActionBar.tsx,ChannelsPanel.tsx,
  DashboardPage.tsx,PipelineTable.tsx,VideoDetail.tsx,format.ts,useDashboard.ts,test-fixtures.ts,
  DashboardPage.test.tsx,VideoDetail.test.tsx}
- LOC: ~875 changed lines + ~870 new lines
- I checked the code against the server, not the plan: collections.{controller,schemas,service}.ts, glossary-term.ts,
  dashboard.{schemas,actions.controller,actions,service,repository}.ts, zod-validation.pipe.ts, folder_exports.py,
  plus nginx.conf, which passes the raw URI, so `%2F` reaches Nest unchanged.
- Checks: `pnpm --filter @reup-flow/web test` passes (14 files, 126 tests). `lint` and `tsc --noEmit` are clean.

## Overall
The wire contract matches the server. I checked each point:
- `collection_id` is left out when no collection is picked, never sent as null.
- A PATCH sends `null` to put a setting back to the shared default.
- `term_zh` goes through `encodeURIComponent`.
- A 400 with a `message` array is kept as a list and split per field.
- The bulk save sends no `generation`. The detail page sends the generation picked, and `save_to_folder` is not in
  `GENERATION_PINNED`.
- `FolderExportView` matches `folderExportView`.
- `videosQuerySchema.collection` is a uuid.

I found no Critical or High issues. There are two real UX defects (Medium) and some gaps in the tests.

## Critical
None.

## High
None.

## Medium

### M1. A saved-folder skip reason hides the live export status until another action runs
`services/web/src/dashboard/VideoDetail.tsx:144` and `:349-353`. `saveSkipped` comes from
`actions.runner.result`. That value stays set until another action replaces it, and polling does not clear it
(`useActionRunner`, useDashboard.ts:68-99). While it is set, the status line shows the skip reason instead of
`d.folder_export`.

Failure scenario:
1. The admin clicks "Lưu vào thư mục" while a request is still pending.
2. The server skips it with "Đã có yêu cầu lưu thư mục đang chờ hoặc đang lưu."
3. The Mac worker then finishes (`done`, with files) or fails.
4. The page keeps showing "Đã có yêu cầu… đang chờ" for as long as the admin stays on it. The admin never sees
   "Đã lưu: …" or "Lỗi: …", which is the one thing this line exists to report (plan: "dashboard hiện trạng thái
   chờ, đang lưu, xong, lỗi").

The same reason is probably also shown by `ActionNotice`.

Fix: always render the `folder_export` line, and show the skip reason as a separate one-off notice. Or clear it
once `d.folder_export.requested_at` changes.

### M2. A `term_zh` error during an in-place edit is dropped without any message
`services/web/src/dashboard/CollectionGlossary.tsx:91-92` and `:213-243`, `:278`. `fieldErrorsOf(err, TERM_FIELDS)` sends a
`term_zh: …` message to `errors.term_zh`. The edit row only renders `errors.term_vi`, `errors.note` and
`errors['']`.

The server re-runs `checkTerm` on the path key for every PUT (collections.controller.ts:57). It also validates the
param with `glossaryParamsSchema`.

Failure scenario: an agent row whose key fails the TS guard. Examples are a row written before a guard change, or
the known gap where Node's Unicode tables differ from Python 15.0 on digits and letters.
1. The admin edits the Vietnamese text and clicks "Lưu".
2. The server answers 400 `term_zh: …`.
3. Nothing appears, the row stays in edit mode, and the click looks like it did nothing.

Fix: in the edit form, map `term_zh` to the `''` slot. Or render `errors.term_zh` under the Chinese cell. Also give
the `errors['']` paragraph at :278 `role="alert"`, as the add form does.

## Low

### L1. A term written `.` or `..` cannot be edited or deleted
`client.ts` `putGlossaryTerm`/`deleteGlossaryTerm`. `encodeURIComponent` does not encode `.`. The URL parser then
removes the dot segment: `/glossary/..` becomes `/api/collections/<id>/`, which answers 404 "Cannot PUT". `checkTerm`
does not reject a dot-only `term_zh`. This is rare with Chinese keys, but the result is a row that can never be
removed. Fix: encode `.` as `%2E` when the segment is only dots. Then check that Nest decodes it back. Express's
`decode_param` does.

### L2. The duplicate check before adding a term compares a different form from the server's key
`CollectionGlossary.tsx:66`. It compares `t.term_zh === newZh.trim()`. The server's key is `pyStrip(...).normalize('NFC')`.
A decomposed form, or Python-only whitespace such as U+0085 or U+3000, misses the "Ghi đè?" confirm. The PUT then
silently overwrites the existing term, which may be an agent term. Fix: compare with `newZh.trim().normalize('NFC')`.
Using U+3000-aware trimming would be better still.

### L3. The bulk "Lưu vào thư mục" hint is only a `title`, and it miscounts
`BulkActionBar.tsx:106`. The plan says "nút bị tắt kèm gợi ý". On the bulk bar the hint exists only as `title` on a
button that may be disabled. Disabled buttons do not get hover in every browser, and touch devices have no hover,
so the admin may never see why it is off.

The hint also counts only the "homeless" videos as skipped. `onRun('save_to_folder')` sends every selected video
(DashboardPage.tsx:95), including ones without a final, which the server also skips. Fix: show the hint as visible
text, as VideoDetail does, and count the skipped videos as the server will.

### L4. The JobForm collection stays picked after a submit
`JobForm.tsx:47-48`. A submit clears the URL and `localizeNow` but keeps `collectionId`. The next channel or video
pasted goes into the same manual collection unless the admin notices. The server sets `collection_manual`, so intake
never corrects it. Fix: reset to "Tự nhận" after a successful submit, or make the choice visibly persistent.

### L5. The settings form sends every field
`CollectionsPanel.tsx:212-219`. The form is seeded once at mount, and polling never re-seeds it. That is
intentional, so a poll does not overwrite what is being typed. But the PATCH carries all six keys. A second tab, or
an edit made between opening and saving, is silently reverted. There is a single admin, so the risk is low. Fix:
send only the keys that differ from `collection`.

### L6. A bulk move does not refresh the collection counts on the detail page
In VideoDetail, a move refreshes only the detail (`onChanged = detail.refresh`). The collection list on that page
stays stale for up to 10 s. This is cosmetic, because VideoDetail does not show `video_count`.

### L7. The web build must not reach production before the new nest
`PipelineTable.tsx:265-269` and `BulkActionBar.tsx:41` read `item.collection.name`/`.id` without a guard. Against
the old nest, which is still the container running now, every row throws and the whole dashboard white-screens.
`?collection=` would also fail with a 400 under the strict schema.

The plan does a maintenance-window rollout with all services stopped, so this is only a deploy-order note for
phase 8. Note that VideoDetail does guard `d.collection`.

### L8. The CollectionsPanel never leaves "Đang tải…" if the first fetch fails
`DashboardPage.tsx:179`. When `listCollections` keeps failing, `collections.data` stays null, so the panel shows
"Đang tải…" forever with no error. Pass `collections.error` through.

### L9. Inputs limit by UTF-16 units while the server counts code points
`maxLength={200}` and `maxLength={512}` count UTF-16 units, so a name with emoji or CJK Extension B characters is
cut short before the server's limit. The server stays authoritative. This is harmless.

## Tests that would still pass with the branch removed

1. **"Collections not loaded" branch is untested.** BulkActionBar.tsx:42 and VideoDetail.tsx:142 both have this
   branch: unknown means the button stays on. If you mutate `c !== undefined && c.save_subdir === null` to
   `c === undefined || c.save_subdir === null`, the bulk tests stay green. Every fixture video's collection is in
   the loaded list. Same for `folder !== undefined && …` in VideoDetail. A recorded decision rests on code that no
   test protects.
2. **The mixed bulk case is not covered.** One homeless and one foldered video should give an enabled button with
   "N video sẽ bị bỏ qua". Removing that branch, so the button is disabled whenever any video is homeless, passes
   every test.
3. **The skip reason replacing the status is not tested.** No test runs a save whose result is skipped and then
   checks that a later poll's `folder_export` shows. That is the M1 defect.
4. **No test covers a `term_zh` error during an in-place edit.** That is M2.
5. **The `collectionId` reset is not tested.** `JobForm.test.tsx` asserts the picked id is sent, but not what
   happens to the selection after a submit (L4). That is a behaviour choice, so the test should pin it either way.

The rest are sound: the field-error tests assert both the right field and the absence on the wrong one. The
client tests assert the exact URL and body. The "no button before a final" test and the "off when no folder" test
would both go red if their branch were removed.

## Edge cases checked and not issues
- ids with special characters: `glossary-vi-${encodeURIComponent(term_zh)}` has no whitespace and is injective, so
  it is valid in `aria-describedby` and unique per row. The server's NFC key rules out canonical duplicates.
- `%2F` in `term_zh`: nginx `proxy_pass` has no URI, so the raw path is passed on. Express matches before
  decoding, so the slash stays inside the param.
- `bgm` with a value like 0.3 or "1e": native validation for `type=number step=0.5` blocks the submit in a real
  browser. jsdom does not do this.
- Bulk move double-click: `setTarget('')` disables the button at once.
- Hooks: `useState` in BulkActionBar runs before the early return. The editor gets `key={current.id}`, so it
  remounts when you switch collection.
- `FINAL_STAGES` with no generation sent: the server takes each video's current generation and skips the ones
  without a final, with a reason.
- A 204 from DELETE: `request` reads the body as text, so an empty body causes no JSON error.

## Recommended actions
1. M1: decouple the skip notice from the `folder_export` status line, and add a test.
2. M2: show `term_zh` and the general edit errors in the edit row, with `role="alert"`, and add a test.
3. Add the two missing bulk save tests (collections unknown, mixed) and the detail test for collections unknown.
4. Phase 8 deploy note: nest before web (L7).
5. Optional: L1 through L5.

## Plan follow-ups
- Implementation step 1: every listed test exists and passes.
- The todos "Panel collection…" and "Form tải, bộ lọc…" are implemented.
- Success criterion "Mở dashboard thật và thao tác được cả luồng" is not done: nest is old, so there was no live run.
- `pnpm build` was not run in this review. `tsc --noEmit` is clean.

## Unresolved questions
- Should the JobForm collection stay picked across submits (L4)? This is product intent.
- Should editing an agent term warn that it turns into a "Tự đặt" term the agent will never update again?

Status: DONE_WITH_CONCERNS
Summary: The wire contract matches the server and there are no Critical or High issues. Two Medium defects remain:
a stale skip reason hides the export status, and a `term_zh` edit error is dropped without a message. The
"collections not loaded" and mixed bulk save branches have no test that would fail if they broke.

## Disposition (implementer, 2026-11-14)

Each fix had its test written first and seen red; each guard was then mutation-checked (removing it turns the test red).

- **M1 fixed.** The status line always shows the newest polled `folder_export`. A skipped click is reported only by the action notice. Test: `a skipped save is reported in the notice, and the status line keeps showing the newest request`.
- **M2 fixed.** A `term_zh` error during an in-place edit appears in that row's 中文 cell, and the general edit error has `role="alert"`. Test: `an edit error about the Chinese key is shown in that row`.
- **L2 fixed.** The "Ghi đè?" check compares keys after trim and NFC, the same normalisation the server applies. Test uses `café` plus U+3000.
- **L3 fixed.** The bulk "no folder" hint is visible text (`bulk-save-hint`), not only a `title`. A new test covers the mixed selection.
- **L4 fixed.** The download form's collection returns to "Tự nhận (mặc định)" after each submit, because intake never moves a hand-placed video back. This is the safer default. The product owner can reverse it.
- **L5 fixed.** The settings PATCH sends only changed fields. An unchanged form sends nothing and says "Không có thay đổi."
- **L8 fixed.** A failed collections load shows "Không tải được collection: …" instead of "Đang tải…" forever.
- **Untested branches covered.** Two new tests cover "collections not loaded → save stays on", one for the bulk bar and one for the detail page. The `c === undefined || …` mutation in both places now turns them red.
- **L1 not fixed (server routing).** A `.` or `..` term cannot be addressed by any URL the client builds, because the URL parser normalises it away before the request leaves. Fixing it needs a server-side key encoding, which is out of scope for this UI phase.
- **L6 and L9 not fixed (cosmetic).** The server stays the authority on lengths.
- **L7 is a deploy-order note for phase 8.** Deploy nest before web. The new web build reads `item.collection`, which the old API does not send.
- **Open product questions.** The L4 default is noted above. Should editing an agent term warn that it becomes a "Tự đặt" term? Not implemented. The existing server behaviour from phase 4 is that a dashboard edit wins.

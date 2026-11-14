# Code Review: Collections phase 4 (collections API and save-to-folder request)

## Scope
- Change under review: the uncommitted working tree on `feat/localization-dubber-code-mode`, checked against `plans/261114-0442-collections/phase-04-collections-api-and-export-request.md`, the locked decisions in `plan.md`, and the open items from `code-reviewer-261114-1610-collections-phase-03.md` (L1: FK violation to 4xx; L4: the file-stem rule).
- New source: `services/downloader/src/collections/{collections.controller,collections.service,collections.schemas,collections.module,glossary-term}.ts`.
- Changed source: `src/app.module.ts`; `src/dashboard/{dashboard.actions,dashboard.actions.controller,dashboard.media,dashboard.repository,dashboard.schemas,dashboard.service}.ts`.
- Contract: new `contracts/glossary-term-cases.json` (48 cases), read by `test/collections/glossary-term.spec.ts` and by `services/localization/tests/agents/test_glossary.py::test_check_term_matches_the_shared_cases`.
- Tests: new `test/collections/*` (3 suites) and changes to 5 dashboard suites.
- Size: about 730 lines of diff plus about 525 lines of new source and about 500 lines of new tests.
- `glossary-term.ts` changed on disk while I was reviewing it. The change was a lint fix (`INVISIBLE_RE` became an alternation, plus an eslint comment), and I reviewed the current version.

## Verification run
- `tsc --noEmit`: clean. `eslint` on the changed src and test files: clean.
- Jest, using isolated test schemas on 5433: 8 suites, 320/320 tests passed (`test/collections/*` and the 5 dashboard suites). I applied no migrations and touched no data in `public`.
- Pytest `tests/agents/test_glossary.py`: 67/67 passed, shared cases included.
- Differential probe, my own run: I fed `checkTerm` (TS, run through ts-node) and `check_term` (Python) the same inputs.
  - Node 22.22.2 ships ICU with Unicode **17.0**. Python 3.12.13 `unicodedata` is Unicode **15.0.0**, and `pyproject.toml` pins `>=3.12,<3.13`.
  - Every input I built from code points added after Unicode 15.0 diverged (see H1).
- Read-only checks on the DB: database ctype is `en_US.utf8` with the libc provider. In that locale `[[:cntrl:]]` matches only C0 and C1, which is exactly the zod `CONTROL` class, so the `save_subdir` and `name` rules match the CHECK constraints.

## Overall assessment
The API side is solid and close to the spec:
- Error mapping is right on every user-error path I tried:
  - FK violation on create gives 400.
  - Glossary PUT on a missing collection gives 404.
  - Bad uuid gives 400, from the pipe.
  - Unknown term on DELETE gives 404.
  - Video not found during a move is reported as skipped.
  - A CHECK violation inside a batch action becomes a per-video "Lỗi hệ thống", not a 500.
- `save_to_folder`:
  - follows the locked file-stem rule (`kind = 'mix' AND mix_episode IS NOT NULL`, read from the video's current collection), which closes phase-3 L4;
  - reuses `resolveRenderKey`, so it can only name the same keys the media links can;
  - takes an SRT only next to a review-folder final;
  - handles double clicks through the partial unique index, and the test proves overlap with an uncommitted competitor.
- Lock analysis for the move and the save is in the table below. There is no cycle.

The weak point is the term guard. It is a faithful port of the regexes, but it runs on a different Unicode database from the Python original. Characters from Unicode 15.1 to 17 make the two sides disagree in both directions, including a confirmed bypass where Python rejects a contact and the dashboard accepts it (H1). Separately, the "no NFC" decision rests on a false premise: the worker's write path does apply NFC (M1).

### Deliberate choices, evaluated
| Choice | Verdict |
|---|---|
| TS port of `check_term` + `find_contacts` | Regex semantics are carefully emulated: `\d`, `\s`, `\b`, IGNORECASE `i`, code-point lengths, and the reasoning that only the phone exception needs `finditer`. Correct only for code points both Unicode versions agree on (H1). |
| PUT strips but does not apply NFC | The plan says Python does not normalise. `GlossaryStore.upsert` does apply NFC (`glossary.py:136-137`, there since 6e26544). See M1. |
| `save_to_folder` with optional `generation` | Correct. A named generation skips the "still current" check and needs a rendered final. Without one, the current generation is used, and Temporal being down still gives a generation from the DB. |
| `resolveRenderKey` shared with media | Good. There is one place that decides which keys may leave the server. The legacy fallback is used only when that very generation's run recorded `output/<id>.mp4`. |
| Partial unique index + `ON CONFLICT DO NOTHING` | Correct, and the concurrent test is real. A `failed` or `done` row lets a new request through. |
| PATCH move in `DashboardActionsController` | Fine. It keeps the read-only controller's "no write route" test meaningful. |
| `FOR KEY SHARE` on the target, `FOR UPDATE` on the videos | Correct. `from` is read under the row lock, so the audit log records the collection each video actually left. |

### Lock analysis (move vs save vs settings PATCH vs enumerate)
- `saveToFolder` takes `FOR SHARE OF v` and `FOR KEY SHARE OF c` in one statement. `moveCollection` takes `FOR KEY SHARE` on the target collection, then `FOR UPDATE` on the videos.
- KEY SHARE is compatible with KEY SHARE and with the NO KEY UPDATE that `PATCH /collections/:id` takes. So the only conflict is on the video row, and each transaction waits for the other's whole statement there. No cycle can form.
- A save racing a move captures one consistent pair: either the old collection and its folder, or the new ones.
- A `save_subdir` edit racing a save is not blocked. The save captures whichever value its snapshot sees, which the spec allows ("captured at request time").
- Enumerate's `moveIntoMixes` filters on `collection_manual = false`, and READ COMMITTED re-checks that filter after a lock wait, so a hand move wins (as analysed in the phase-3 review).

## Critical
None.

## High

### H1. The term guard disagrees with `check_term` on characters added after Unicode 15.0, including a contact bypass
- `src/collections/glossary-term.ts:27-28` (`WORD`/`B` use `\p{L}\p{N}` from Node's Unicode 17), `:53-58` (`\p{Nd}`), `:66-68` (`normalize('NFKC')`). On the Python side, `unicodedata` is 15.0.0 (`pyproject.toml` pins 3.12).
- **Bypass (dashboard accepts, Python rejects). I ran each pair:**
  - `checkTerm('abc', 'ok', '\u{1E5D0}shop dot com')` gives `null`. Python gives "note contains a URL, domain name, e-mail address or phone number". U+1E5D0 is an Ol Onal letter (Unicode 16). Node sees a word character, so there is no `\b` before `shop`. Python sees `Cn`, a non-word character, so the boundary matches.
  - `'shop chấm vn\u{1E5D0}'`: same result, with the trailing boundary.
  - `'\u{1E5D0}www.abcxyz.qq'` in `term_vi` or `note`: the `\bwww\.` URL branch misses. The TLD is not in the list, so the domain pattern cannot catch it either.
- **The other direction (dashboard rejects, Python accepts):**
  - Outlined digits U+1CCF0 and up (Unicode 16): NFKC turns them into ASCII digits in Node only. So `'x\u{1CCF1} y'` fails the digit rule, and seven of them in a row fail as a phone number.
  - Garay digits U+10D40 and up: `\p{Nd}` in Node only.
- **Scenario:** The API key ships in the web bundle, and the spec names `check_term` as one of the two real boundaries. Anyone who can reach the dashboard API can therefore store a dashboard term carrying a spoken domain. That term then reaches the Localizer prompt as a trusted, person-set glossary entry. `wrap_untrusted` and the output-line contact check (`contacts_missing_from_source`, which uses Python's tables and would catch a verbatim copy) reduce the harm. They do not restore the stated boundary. The reverse direction only produces false 400s on exotic input.
- **Why the tests missed it:** none of the 48 shared cases contains a non-BMP or post-15.0 code point. A 180k-case fuzz drawn from the BMP or older blocks cannot reach this class of input.
- **Fix (pick one or combine):**
  - (a) Enforce at the real boundary. Have the Python side re-run `check_term` on rows it reads (`GlossaryStore.lookup`, and the phase-5 lookup) and drop any row that fails. This also covers terms written directly in the DB. It is the only option that does not depend on two Unicode databases staying in step.
  - (b) Make the TS side refuse what it cannot judge. Reject any term containing a code point that is unassigned (`Cn`) in Unicode 15.0. Generate the range table once from Python (`unicodedata.category(ch) == 'Cn'` while `/\P{Cn}/u` matches in Node) and record where it came from in a comment. Rejecting them closes both directions.
  - Either way, add cases with U+1E5D0 and U+1CCF1 to `contracts/glossary-term-cases.json`, so a future upgrade of Node or Python fails loudly. Add one note to the header comment of `glossary-term.ts` that the emulation assumes matching Unicode versions.

## Medium

### M1. The "Python does not normalise NFC" premise is false; dashboard terms are stored in a different form from agent terms
- `src/collections/glossary-term.ts:137-153` (the transform strips but applies no NFC); `test/collections/glossary-term.spec.ts:45` (title: "does not NFC-normalise: the worker check does not either"); phase-04 line 52.
- On the Python side, `GlossaryStore.upsert` (`activities/glossary.py:136-137`) runs `unicodedata.normalize("NFC", term.strip())` on `term_zh` and `term_vi` **before** `check_term`. `check_term` itself applies no NFC, which is what the parity cases test. The stored row is NFC. The red-team note only removed the sentence "chuẩn hóa NFC giống Python". It did not establish that the worker skips NFC.
- **Effects:**
  - **Key identity.** `(collection_id, term_zh)` is the conflict key. A dashboard term entered in a non-NFC form, for example a CJK compatibility ideograph (U+F900-FAFF, which NFC maps to the unified ideograph) or a decomposed Latin letter in a mixed term, does not conflict with the agent's NFC row. Both rows then exist, `PUT` does not override the agent's term, and the agent's `ON CONFLICT DO NOTHING` does not see the person's term. That breaks the success criterion "thuật ngữ tự đặt ... không bị agent ghi đè".
  - **Length.** Vietnamese typed through a combining-mark input mode (Unikey "Unicode tổ hợp", or text pasted from macOS file names) costs 2-3 code points per letter. A term Python would accept at 30 visible characters is rejected here at 64 code points.
  - The prompt-side term check (`localizer_single.missing_terms`) applies NFKC, so it is not affected.
- **Fix:** apply `.normalize('NFC')` after `pyStrip` on `term_zh` and `term_vi` in the transform, exactly as `upsert` does, and NFC-normalise the DELETE key. Rename the test, invert what it asserts, and correct phase-04 line 52. This touches a sentence of an accepted plan, so the lead should confirm it with the user, presenting the evidence above (rule: do not silently reverse a plan decision).

## Low

### L1. Bulk `save_to_folder` with an explicit `generation` applies one generation number to every selected video
- `src/dashboard/dashboard.schemas.ts:96-103`, `dashboard.actions.ts:164-168`.
- `generation` is a property of one video, but the schema accepts it with up to 100 ids. "Save g2" across a selection quietly picks unrelated renders, or skips videos where g2 was never rendered.
- **Fix:** in `superRefine`, reject `save_to_folder` with `generation` and more than one id (as `open_studio` and `approve` do), or document that phase 7 must omit it for bulk saves.

### L2. `files` and `error` in `folder_export` are untyped pass-throughs
- `dashboard.repository.ts:149-160` (`files: unknown`), `dashboard.service.ts:438-452`.
- Phase 6 decides the JSON shape and the error text, and phase 7 renders them. If the Mac worker writes absolute paths under `REVIEW_WORKER_EXPORT_ROOT`, or raw exception text, into `files` or `error`, they go to anyone holding the public key.
- **Fix:** have phase 6 define the shape (for example names relative to the subdir) and keep host paths out of `error`. Optionally narrow the type here once phase 6 lands.

### L3. A lone surrogate is accepted and stored as U+FFFD
- `glossary-term.ts` (no `\p{Cs}` check); the same applies to `name` and `save_subdir` in `collections.schemas.ts`.
- **Scenario:** JSON `"\ud800"` parses to a lone surrogate. It passes every rule, and node-postgres encodes it as U+FFFD, so the stored term differs from the one that was checked, and a later DELETE by the original key misses. Python would raise on encode instead.
- **Fix:** reject `/\p{Cs}/u` in the term schema and in `nameSchema`/`saveSubdirSchema`.

### L4. Without `generation`, the save path pays for a full video read and a Temporal call just to find the current generation
- `dashboard.actions.ts:170-176`.
- `repo.video()` runs the full `SELECT_VIDEOS` with all its JSON aggregates, and `openWorkflows()` waits up to the Temporal timeout when Temporal is slow. That is acceptable for a click, and the result degrades to DB-only. Worth one line in phase 7: send `generation` from the detail view, where it is known.

### L5. A review final with no valid SRT is queued silently without one
- `dashboard.actions.ts:546`.
- This is correct for legacy finals. For a review-folder final whose `srt_key` is missing or fails the folder check, the row gets `srt_key = NULL`, and the dashboard cannot tell, because keys stay on the server side, until phase 6 reports `files`.
- **Fix:** if phase 7 should warn about this, expose a boolean `has_srt` in `folder_export`.

## Edge cases checked (no defect)
- `save_subdir` rules: zod matches the CHECK constraint (empty, `.` and `..` segments, leading or trailing `/`, C0/C1, 512 code points). DB locale verified.
- `bgm_offset_db` steps of 0.5 are exact in `real`. `cover_opacity_pct` is an integer. Colours are stored lower case. Names are trimmed and count 1-200 code points, which matches `char_length`.
- PATCH builds column names from a fixed list. An empty patch gives 400, a missing id gives 404, and `null` writes NULL.
- PUT takes `term_zh` from the path and rejects it in the body (strict). The term rules run on the stripped values. An agent row overwritten by a person becomes `workflow_id = 'dashboard'` and keeps `created_at`. A collection deleted mid-request gives 404.
- `file_stem`: `E01`, `E10`, `E100`, and the plain stem for a 合集 episode moved by hand into a manual collection. All satisfy `folder_exports_file_stem_check`.
- `GET /api/dashboard/videos?collection=` accepts an upper-case uuid (lowercased by the schema) and rejects an empty value or a non-uuid with 400. The filter is served by `videos_collection_recent_idx`. The inner `JOIN collections` is safe because `videos.collection_id` is NOT NULL.
- The Python test resolves `contracts/` with `parents[4]` to the repo root, and CI checks out the whole repo.

## Recommended actions
1. H1: add the read-side `check_term` filter in Python (or the Unicode-15 `Cn` rejection in TS) and the post-15.0 cases to the shared contract.
2. M1: get the user's confirmation, then add NFC after the strip, fix the test title and assertion, and correct phase-04 line 52.
3. L1: restrict `generation` to a single id for `save_to_folder`, or document the rule for phase 7.
4. L2 and L5: carry them into phase 6 and phase 7 as contract notes.
5. L3: reject lone surrogates.

## Plan follow-ups (do not edit the plan here)
- The phase-4 todo items look implemented: route and validation tests, glossary parity, the collections module, filter, move, `save_to_folder` and latest-save status. The phase file still says `status: todo`, and `plan.md` lists phase 4 as Pending.
- Phase-3 L1 (FK to 4xx) is closed for the new write paths. Phase-3 L4 is closed by `fileStemOf`, which keys on the collection kind.
- "Related Code Files" lists `dashboard.controller.ts`. The move went to `dashboard.actions.controller.ts` on purpose, so that entry is stale.
- The success criterion asks for `pnpm --filter @reup-flow/downloader test` and `lint` to be green on the full suite. I ran only the targeted suites.

## Metrics
- Type check: 0 errors. Lint: 0 issues on changed files.
- Touched suites: 320/320 (jest) and 67/67 (pytest).

## Unresolved questions
- M1: should dashboard terms be NFC-normalised like the worker's writes? (I recommend yes; it reverses one plan sentence.)
- H1: which side should own the Unicode-version risk: a Python read-side re-check (defence in depth), a TS rejection table, or both?
- L1: is bulk save with one explicit generation a supported use?

## Xử lý (implementer, 2026-11-14)

Mỗi mục sửa đều có test đỏ trước (9 test jest đỏ cho H1, 3 cho L1 và L3), rồi mới sửa code.

| Mục | Xử lý |
|---|---|
| H1 | Đã sửa bằng cách mô phỏng đúng Python, không từ chối: `src/collections/python-unassigned.ts` liệt kê 707 khoảng code point mà `unicodedata` 15.0.0 của Python coi là chưa gán (Cn). `glossary-term.ts` không coi chúng là chữ, chữ số hay ký tự từ, và NFKC từng đoạn giữa chúng, nên kết quả và cả câu lý do đều trùng `check_term`. Thêm 7 ca (U+1E5D0, U+1CCF1, U+10D41, U+1E5EE) vào `contracts/glossary-term-cases.json`. Test Python mới `test_the_dashboard_copy_knows_which_code_points_this_python_leaves_unassigned` so bảng với `unicodedata` và với phiên bản Unicode, nên nâng Python là đỏ cho tới khi sinh lại bảng. Fuzz vi sai thêm 190k chuỗi, có code point ngẫu nhiên trên mọi plane: 0 khác biệt. Phương án (a), kiểm lại ở phía đọc của Python, thuộc phase 5 (viết lại `GlossaryStore`); ghi lại cho phase 5. |
| M1 | Chưa đổi code, cần người dùng quyết: plan ghi "không chuẩn hóa NFC vì phía Python không chuẩn hóa", nhưng `GlossaryStore.upsert` có NFC trước `check_term`. Đã sửa tên test và chú thích cho đúng sự thật; hành vi vẫn theo plan. |
| L1 | Đã sửa: `save_to_folder` có `generation` thì chỉ nhận đúng một video (giống `approve`, `open_studio`); thao tác hàng loạt bỏ `generation` và mỗi video lưu generation hiện tại. |
| L2 | Ghi cho phase 6: định nghĩa shape `files` (đường dẫn tương đối dưới thư mục con) và không để đường dẫn máy Mac lọt vào `error`. |
| L3 | Đã sửa: từ chối lone surrogate trong thuật ngữ, ghi chú, tên collection và `save_subdir`. |
| L4 | Ghi cho phase 7: trang chi tiết gửi `generation` khi đã biết. |
| L5 | Ghi cho phase 7: nếu cần cảnh báo thiếu SRT thì thêm `has_srt` sau khi phase 6 chốt `files`. |

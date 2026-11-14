# Code review: collections phase 5 (run settings in localization)

Branch `feat/localization-dubber-code-mode`, uncommitted working tree, reviewed 2026-11-14.
Spec: `plans/261114-0442-collections/phase-05-run-settings-in-localization.md`, the locked
decisions in `plan.md`, phase 2 review L5, phase 4 review H1 (option a).

## Scope

- Source: `contracts/{collection.py (new), models.py, review_project.py, spec_hash.py, __init__.py}`,
  `starter/outbox_starter.py`, `activities/{glossary.py, localizer_ports.py, assemble_audio.py,
  revoice_caption.py, layout_captions.py, caption_images.py, review_project.py}`,
  `agents/{localizer.py, localizer_single.py}`, `workflows/video_localization.py`, `workers/run.py`,
  `review_bridge/repository.py`, two JSON schemas, `.env.example`.
- Tests: 3 new files, about 15 edited tests and doubles. About 970 lines added, 145 removed.
- I checked these paths against the code, not against the summary: the starter (`VIDEO_SQL`,
  `_start_input`, canary), the glossary SQL and the migrations behind it
  (`261114-collections.sql`, `261114-glossary-collection-scope.sql`), the two audio mix callers,
  every `project_html` caller, `Repository.prepare`, `export_revision` (used by
  `review_worker/final_render.py:300`), and how compose passes env to `worker-agents` and
  `worker-media`.

## Overall assessment

The change is close to the spec and the replay and back-compat story holds. The input, the
manifest and the edit spec each add optional fields only. No activity signature changed, so
replay needs no `workflow.patched`. An old spec keeps its hash, which is pinned by a literal
digest. A project without settings produces byte-identical HTML, pinned by a literal sha256.
Settings survive a Studio save and a revoice, because `prepare` uses `model_copy` and passes
the colours from the base manifest. They also reach the Windows final render, because
`export_revision` stages the stored HTML as it is and never rebuilds it. The SQL is correct and
was run as role `localization`. The new tests are real: each one I reasoned through goes red if
its line of code is removed.

There are two real gaps. First, the review flow's "worker cover" fallback reads env that compose
never gives to `worker-agents`, and `.env.example` now says the opposite (M1). Second, the
read-side `check_term` filter leaves one path open: the "exists" reply sends a stored `term_vi`
to the model without checking it, which contradicts the new module docstring (M2).

## Critical

None.

## High

None.

## Medium

### M1. The review flow's cover fallback never sees `COVER_COLOR`/`COVER_OPACITY`, and `.env.example` claims it does

- `ReviewProjectActivity` is built in the `agents` role (`workers/run.py:229-235`) and fills a
  partial collection cover from `settings.cover_color` and `settings.cover_opacity`.
- `docker-compose.yml` passes env through an explicit allowlist. `COVER_COLOR` and
  `COVER_OPACITY` appear only under `worker-media` (`docker-compose.yml:599-600`), not under
  `worker-agents` (`:496-579`). `docker-compose.override.yml:16` does not add them either.
- As a result, the review flow always completes a partial cover with the code default
  (`#101010`, 0.92), while the legacy flow uses the configured value. If an operator sets
  `COVER_COLOR=#000000`, a collection that sets only opacity gets `#000000` in legacy renders and
  `#101010` in review projects, which is the production path.
- The new `.env.example` text ("read by the media worker for rendered videos, and by the agents
  worker for review projects") is false for the shipped compose file.
- `test_the_review_project_completes_a_collection_cover_with_the_configured_one` builds
  `Settings(...)` directly, so it cannot catch this. No test compares the compose allowlist with
  what each role reads.
- Fix: add `COVER_COLOR: ${COVER_COLOR:-#101010}` and `COVER_OPACITY: ${COVER_OPACITY:-0.92}` to
  `worker-agents`. If the Mac agents worker runs under pm2, check its env file too. Phase 8 should
  verify the value from inside the running container.

### M2. The read-side guard has a hole: the "exists" reply sends an unchecked stored `term_vi` to the model

- The new docstring in `glossary.py` says a row read back is checked again, so that "nothing it
  refuses reaches the prompt". `lookup()` does filter.
- `upsert()` does not. When the term already exists, it returns `existing[0]` from
  `EXISTING_SQL` without calling `check_term` (`glossary.py`, the `existing` branch).
  `localizer.py:297-301` then sends it to the model as "use that", wrapped in `wrap_untrusted`.
- A dashboard row that got past the TS guard (the Unicode-version risk in phase 4 H1, or a row
  written by hand) reaches the prompt whenever the agent tries to save the same `term_zh`. That
  only needs the term to appear in the transcript.
- Phase 4 made the TS guard emulate Python's Unicode table, so the likelihood is low. This is a
  defence-in-depth gap, plus a docstring that claims an invariant the code does not hold.
- Fix: have `EXISTING_SQL` also select `term_zh` and `note`, and run `check_term` on that row. If
  it fails, return `UpsertOutcome("exists", "not changed: the collection already fixes this
  term", None)` so the tool returns the message without the text. Add a test that stores a refused
  dashboard row, upserts the same `term_zh`, and asserts that `existing is None`.

### M3. Deviations from the phase file are not recorded there

1. Opacity-only cover (your decision). In the review flow, a collection that sets only
   `cover_opacity_pct` gets a solid box in the worker colour. The phase text only says "có
   `cover_color` thì ...". I agree with the choice: it matches the legacy flow (env colour plus
   the collection's opacity), and a setting a person made should have a visible effect. It only
   holds if M1 is fixed, because otherwise "the worker colour" means different things in the two
   flows. Record it in the phase file.
2. Manifest `bgm_offset_db` is a float, not an integer (your decision). The phase says the
   manifest fields are "đều dùng HexColor hoặc số nguyên". I agree with the float. The column is
   `real` with 0.5 steps, so an integer would round away half-dB values. Every allowed value is
   exact in binary, and the manifest is written only by Python, so its bytes stay stable. The
   "no float" rule protects the canonical EditSpec hash, and the offset never enters the spec.
   Record it.
3. `review_bridge/service.py` and `contracts/export.py` are listed as "Modify" but did not need
   changes: `prepare` covers Studio, and export regenerates the schemas generically. This is
   fine. Note it so the phase checklist does not look incomplete.

## Low

### L1. A missing collection returns "rejected" with a misleading message and leaves no log

- I agree with this decision (it resolves phase 2 L5). A retry cannot fix either a missing
  collection or a channel with no channel collection, so failing the activity would only burn
  retries and then fail the whole run over an optional glossary write.
- Two problems remain. First, the same message ("this video's collection no longer exists") is
  used for the `NotNullViolation` case, where the collection never existed for that channel.
  Second, neither case is logged, and `lookup()` returns an empty glossary for the same run
  without a word. An operator cannot see that a run translated with no glossary.
- Fix: log a warning with `collection_id`/`channel_id` in the `except` branch. Optionally use two
  messages.
- Catching `NotNullViolation` broadly is acceptable here. The other NOT NULL columns are always
  supplied (`aweme_id` and `workflow_id` come from deps) or are guarded (`term_vi` by
  `check_term`).

### L2. The tool docstrings still say "channel glossary" (your decision)

- `glossary_lookup` ("The channel glossary ... the channel's first 100 entries"), `glossary_upsert`
  ("a term of this channel") and the "exists" reply ("the channel already fixes") now describe
  the wrong scope.
- The behaviour risk is small, because the model does not choose the scope. Leaving the prompts
  alone to avoid prompt churn is defensible.
- The inconsistency is now inside one file, though: `GlossaryStore` says "the collection already
  fixes this term" while the tool reply says "the channel already fixes". Change the wording when
  the prompts are next touched.

### L3. The pre-existing red test `test_deps_carry_keys_and_numbers_never_video_content` now misses two keys

- Its key set lacks `source_video_key` (this is why it is red at HEAD) and now also
  `collection_id`.
- Because it is red, it guards nothing. If someone later adds a content-bearing field to
  `LocalizerDeps`, nothing will notice.
- Add both keys so it goes green and guards again. The fix is one line, and it belongs to this
  change because this change adds a field.

### L4. Upgrade order: every manifest written by new code carries the new keys

- `model_dump_json()` writes `"caption_color":null,...` even for a run with no collection. Any
  old `review-bridge` or Windows render worker therefore rejects every manifest written after
  the deploy, not only manifests with settings (`extra="forbid"`).
- `plan.md` already requires upgrading every component before the starter is turned back on.
  Phase 8 should list the Windows render worker and the Mac review worker explicitly, and check
  their versions before any new project is built.

### L5. Small points

- `LOOKUP_LIMIT` is applied before the read-side filter, so refused rows reduce the 100 that
  remain. This is harmless in practice.
- `MAX_MUSIC_OFFSET_DB` (contract) and `MAX_MUSIC_GAIN_DB` (mix) are two separate 24.0 constants.
  The clamp in `music_gain_db` makes them independent, so this is cosmetic.
- `test_render.py` uses `Image.getdata`, which is deprecated in Pillow 14.

## Edge cases checked

- Old input without `collection`, in flight or replayed: decodes. The glossary falls back to the
  channel collection through `COALESCE`, which runs in an activity, so replay is not affected.
  Music offset is 0, the legacy layout writes yellow (the decided unified default), the review
  manifest gets no settings, and the HTML is byte-identical (pinned).
- A legacy workflow that finished `layout_captions` before the deploy and renders after it: the
  stored spec has no colour, so it renders white with its old hash. This is the intended
  behaviour.
- Collection edited after dispatch: the input keeps its snapshot. The starter test proves this,
  and also proves that the next dispatch reads the new values.
- Collection deleted mid-run: in practice impossible while `videos.collection_id` (no cascade)
  references it. If it happens anyway, the FK refuses the write and the agent gets "rejected".
  The glossary FK has no `ON DELETE`, so deleting a collection that has terms fails. That is a
  phase 7 concern.
- Video moved to another collection mid-run: the run keeps its snapshot's glossary and look.
  This is consistent.
- `bgm_offset_db` column is `real` with a 0.5-step CHECK, so psycopg returns an exact float that
  `MusicOffsetDb` accepts. `cover_opacity_pct` is `smallint`, so it arrives as an int.
- FK check privilege: `localization` has only SELECT on `collections`. The RI check runs as the
  owner of the referenced table, and `glossary_db` connects with `role=localization`, so both
  the accepted insert and the FK refusal were exercised under the real role.
- CSS injection through colours: every value written into `project_html` comes from a validated
  `HexColor` (snapshot, manifest) or from `Settings` (its validator lower-cases and pattern-checks
  it).
- Revoice: `_mix` uses the head manifest's `bgm_offset_db`, and the balance is recomputed on the
  full remix, so the offset holds (measured: -6 dB plus or minus 1).
- Studio import path (`service.py:77-91`): visual edits are captured and reapplied over a freshly
  generated `project_html`, which uses the base manifest's colours.

## Tests

- My run: 795 passed across the new and edited tests, `tests/replay` and the schema drift test.
- Broader run (`tests/agents tests/review_bridge tests/workflows tests/review_worker
  tests/integration tests/activities/test_review_project.py`): 752 passed, 7 failed. None of the
  failures comes from this change:
  - Three are failures you already listed: `test_deps_carry_keys...`,
    `test_unimported_studio_source...` and `test_font_parity`.
  - `test_scan_studio_worker::test_helper_rejects_bad_identity...` fails because `node` is not on
    PATH.
  - Three are not on your list but fail the same way at HEAD. I checked them in a throwaway
    worktree of HEAD: `test_one_request_agents::test_recorded_review_policy_uses_single_context_and_replays`,
    `test_one_request_agents::test_contextual_missing_id_cannot_become_an_implicit_omission`
    (the `prep_worker.py:99` assertion that `source_video_key` is set) and
    `test_prep_stage::test_the_real_clips_final_cover_hugs_the_text`. Add them to the known
    pre-existing list.
- Coverage of each change, by whether the test goes red if the code is removed:
  - prepare colours: Studio test.
  - revoice offset: measured test.
  - dashboard-first ORDER BY: 100 older rows push the dashboard row out without it.
  - read-side filter: test present.
  - hash pop: literal digest.
  - `**settings` in the manifest: field asserts.
  - FK and NOT NULL catch: outcome asserts.
  - `VIDEO_SQL` join: starter and grants tests.
  - `collection_id` in deps: workflow test, checked against the activity history.
  - painter colour: pixel test in both directions (yellow, and white for None).
- Vacuous tests: none found.
- One weak test: `audio_project` injects the settings into the base manifest with `model_copy`
  instead of building it through `build_review_project`. The builder path has its own tests, so
  this is acceptable.
- Missing tests: M1 (compose env per role) and M2 (the existing-term reply).

- Static checks: I did not run ruff or mypy.

## Plan follow-ups

- Todo items look complete: contract, starter, glossary with FK and old-key mapping, music
  offset including revoice, colours in both flows including Studio, schemas, replay and test
  doubles.
- Before marking the phase done, fix M1 and M2, and record the deviations in M3.

## Unresolved questions

- Does the agents-role worker on any non-compose host (pm2) build review projects? If so, its env
  needs `COVER_*` too.
- Should a run whose glossary scope resolves to nothing fail loudly at the start of the run,
  instead of just writing the warning suggested in L1? This is a product call.

## Xử lý (implementer, 2026-11-14)

Mỗi mục sửa có test đỏ trước (1 test jest, 3 test pytest), rồi mới sửa; mỗi nhánh mới được đột biến lại và
làm test đỏ.

| Mục | Xử lý |
|---|---|
| M1 | Đã sửa: `docker-compose.yml` truyền `COVER_COLOR`/`COVER_OPACITY` cho `worker-agents`, cùng mặc định với `worker-media`. Test jest mới `the agents worker gets the same cover setting as the media worker` (đỏ trước khi sửa compose); allowlist của `worker-agents` trong `compose-constraints.spec.ts` thêm hai biến. Test allowlist tổng vẫn đỏ vì drift có sẵn (`ZEROTTS_VOICE_ID` ở starter), nó dừng ở service đầu tiên lệch nên mới cần test riêng. pm2 không chạy role `agents` (chỉ voice, ingest, review). `.env.example` giờ đúng. |
| M2 | Đã sửa: `EXISTING_SQL` lấy cả `term_zh`, `note`; câu trả lời "exists" chỉ kèm `term_vi` khi `check_term` nhận dòng đó, nếu không thì `existing = None`. Test `test_a_stored_term_the_guard_refuses_is_not_shown_when_the_agent_repeats_it`. |
| M3 | Đã ghi vào phần "Ghi chú triển khai" của phase 5: độ mờ không kèm màu, `bgm_offset_db` số thực, các file "Modify" không cần sửa. |
| L1 | Đã sửa: hai thông điệp (`...collection no longer exists` cho FK, `...channel has no collection` cho NOT NULL) và `log.warning` kèm `collection_id`/`channel_id`. Test kiểm thông điệp và bản ghi log. |
| L2 | Giữ nguyên (quyết định đã nêu): không đổi prompt trong phase này. |
| L3 | Không sửa: người dùng đã ghi rõ test này là lỗi có sẵn, ngoài phạm vi, không sửa. Ghi lại rằng khi sửa cần thêm cả `source_video_key` và `collection_id`. |
| L4 | Ghi cho phase 8 (file phase 8 không thuộc phase này): nâng `review-bridge`, render worker Windows và worker Mac trước khi bật starter, vì mọi manifest mới đều có bốn khóa mới. |
| L5 | Đã đổi `Image.getdata` sang `tobytes()` trong test pixel. Hai hằng 24 dB và giới hạn 100 trước bộ lọc giữ nguyên (vô hại, như review nhận định). |

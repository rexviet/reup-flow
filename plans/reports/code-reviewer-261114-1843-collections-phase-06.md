# Code review: collections phase 6 (Mac folder export worker)

Scope: uncommitted diff on `feat/localization-dubber-code-mode`.
`review_worker/folder_exports.py` (new), `test_folder_exports.py` (new), `review_worker/main.py`,
`review_worker/config.py`, `storage/object_store.py` (+`download_fileobj`), `tests/test_object_store.py`,
`scripts/lib/review-worker-env.mjs`, `ecosystem.config.cjs`, `scripts/tests/review-studio-request.test.mjs`.

## Verification run

- `pytest tests/review_worker/test_folder_exports.py tests/test_object_store.py`: 38 passed.
- `pytest tests/review_worker`: 92 passed, 1 failed (`test_helper_rejects_bad_identity_and_reports_stderr`:
  `node` not on the uv PATH. This failure exists outside this diff and is unrelated).
- `node --test scripts/tests/review-studio-request.test.mjs`: 11/11. `ruff check` and `ruff format --check`: clean.
- Mutation checks (a pytest plugin in the scratchpad; no source edits):
  - CLAIM_SQL without `SKIP LOCKED`: the overlap test fails.
  - CLAIM_SQL without `FOR UPDATE SKIP LOCKED`: the overlap test fails.
  - `O_NOFOLLOW` removed from the directory walk: the subdir-symlink test and the stale-cleanup symlink test fail.
  - Conclusion: the overlap test is genuine. It proves the first transaction is still open while the second claimer finishes.
- Reproduced finding M1 with a scratch script: `clean_stale` raises `PermissionError` (EPERM).

## Findings

### High

**H1. A missing export root stops the whole review worker from starting** (`config.py:88-90`)
- Scenario: `REVIEW_WORKER_EXPORT_ROOT=/Volumes/Video/reup` is set (the test uses exactly that kind
  of path) and the drive is not mounted at boot or after a pm2 restart.
- What happens: `from_environ` raises `ConfigError` and the worker exits. pm2 then gives up after
  `max_restarts: 10`. The heartbeat, Studio-open requests and the review scan all stop, and the
  dashboard shows the worker as offline. One optional feature takes down the core one.
- The runtime path already handles a missing root (each row fails with "Thư mục gốc … không tồn
  tại"). Only the startup check is fatal.
- Fix: at startup, require only `is_absolute()`. If the directory is missing, log a warning and let
  the per-request path report it. Alternatively, leave rows `pending` while the root is missing (see L1).

### Medium

**M1. A directory whose name matches the temp pattern blocks every export indefinitely** (`folder_exports.py:272-276`)
- Cause: `clean_stale` unlinks every entry matching `PART` without checking the entry type, and only
  suppresses `FileNotFoundError`. On macOS, `os.unlink` on a directory raises `PermissionError`
  (EPERM, reproduced).
- Why it is reachable: `saveSubdirSchema`, the DB CHECK and `check_subdir` all accept a segment such
  as `.reup-export-<32 hex>.part`.
- Scenario:
  1. An API-key holder sets a collection's `save_subdir` to `A/.reup-export-000…0.part` and
     requests a save. The worker creates that directory.
  2. Any row targeting `A` then has its lease expire. A routine worker restart mid-export is enough.
  3. From then on, every drain raises in `clean_stale` before `claim`. The export thread logs
     `folder exports failed: PermissionError` every 5 s.
- Effect: no export is ever claimed again. The expired row is never reclaimed either, so the block
  does not clear itself.
- Fix:
  - Before unlinking, `os.stat(name, dir_fd=fd, follow_symlinks=False)` and unlink only `S_ISREG`.
  - Suppress `OSError` for each entry, logging only the errno name.
  - Additionally, `check_subdir` could reject segments that start with `.reup-export-` (or any
    leading dot).
- Add a test with a planted directory of that name.

**M2. The export root reaches the worker only from the interactive shell, and this is not documented** (`ecosystem.config.cjs:85-89`)
- The value is read from `process.env` when pm2 evaluates the ecosystem file. It is not read from
  `.env`, and `start-all.sh` does not source `.env`.
- Scenario: the operator puts the variable in `.env`, or later runs `start-all.sh reload` from a
  shell without it.
- Effect: requests stay `pending` forever. The only signal is a single log warning; the dashboard
  shows nothing.
- Phase 8 plans the docs, but the mechanism itself is fragile. It is also unclear whether
  `pm2 reload --update-env` drops a key the ecosystem no longer sets or keeps the old value.
- Fix: document the variable in the phase-8 docs. Prefer reading it from `.env` through the existing
  `workerEnv` path, or make the dashboard show "Mac chưa cấu hình thư mục lưu" when the heartbeat
  reports no export root.

### Low

**L1. A drive unplugged while the worker runs fails every pending request** (`main.py:172-175`, `folder_exports.py:249-252`)
- `disk_ok` raising `OSError` is treated as "not low", so every row is claimed and fails with
  "Thư mục gốc … không tồn tại". The user must click every row again.
- This is the stated design and is acceptable, but leaving rows `pending` while the root is missing
  would be friendlier and would match the "unset ⇒ pending" rule.

**L2. The shutdown comment is wrong** (`main.py:214-215`)
- It says "the next start removes its temp file". In fact the file is removed only after the
  30-minute lease expires, because `clean_stale` selects expired rows only. The row also shows
  `exporting` for up to 30 minutes after a restart.
- Fix the comment, or on stop settle the in-flight row back to `pending` (claim-guarded) so it is
  retried at once.

**L3. Shutdown can exceed pm2's `kill_timeout` when the Mac runs every role** (`main.py:203-216`)
- Joins: render 60 + 30 s, Studio 5 s, export 60 s, so up to 155 s against `kill_timeout: 90000`.
- With `studio` only (the current config) the total is 65 s, which is fine. If rendering is
  re-enabled on the Mac (the comment in the ecosystem file describes how), pm2 SIGKILLs the process
  during the export join.
- The render requeue happens before that, so the damage is limited. Cap the export join at the time
  remaining.

**L4. Failures are hard to diagnose** (`folder_exports.py:229`, `main.py:100`)
- Only `type(exc).__name__` is logged. For `OSError` and `ClientError`, log the errno name or the
  S3 error code too. Neither leaks a path.

**L5. Test gaps**
- No test covers the 403/AccessDenied message mapping (the known production condition).
- No test covers a lost claim between the MP4 and the SRT (renew fails before the second file).
- No test covers `stopping` ending `drain`.
- `test_a_symlink_planted_at_the_temp_name_is_not_followed` passes on `O_EXCL` alone. `O_NOFOLLOW`
  there is redundant defense, which is fine, but the test does not show that `O_NOFOLLOW` is needed.

**L6. The parent directory is not fsynced after `os.replace`** (`folder_exports.py:175`)
- A power cut can lose the rename even though the row says `done`. Low impact: the user can save again.

## Design decisions: assessment

- **Relative `files`:** correct. It satisfies phase-4 L2. Errors are fixed sentences: `OSError` uses
  only the errno name, `ClientError` only the S3 code. No absolute path leaks into the DB, which was verified by tests.
- **O_NOFOLLOW walk instead of realpath:** correct and stronger, with no TOCTOU. The root itself is
  opened following symlinks, which is right because the operator configures it.
  `mkdir` then `open(O_NOFOLLOW)` handles a planted symlink or file (ELOOP/ENOTDIR → rejected).
  `os.replace` on a planted symlink or hardlink at the final name replaces the directory entry and
  does not write through it.
- **Cleanup at each drain:** correct for a single serial exporter. It runs before `claim` in the same
  thread, so it never races this process's own `.part`. With two studio workers sharing a root, one
  could delete the other's live `.part` in a shared subdir (episodes share a subdir). The other
  export would then fail with ENOENT and need another click. There is only one Mac, so this is acceptable.
- **Optional export root:** fine when unset. When set but missing, it is fatal (H1).
- **Error policy (non-DB → failed, DB → propagate):** correct. A re-export is idempotent.
  `Session` retry after a lost response can report `lost` for a row that actually settled. That only
  affects the log line.
- **Lease 30 min, renewed per file:** acceptable. `RENEW_SQL` does not require an unexpired lease, so
  a slow file still renews unless the row was reclaimed.
- **Claim:** `FOR UPDATE SKIP LOCKED` in the subquery matches `final_render.py`. The overlap test
  proves it, and the mutations above confirm it.

## Key-trust check

- `check_key` is equal to or stricter than `resolveRenderKey`: plain name, no leading dot, correct
  extension.
- It accepts `output/<aweme>.mp4` without the dashboard's "run recorded exactly it" check. This is
  acceptable: only the dashboard inserts rows, after `resolveRenderKey`, and `localization` cannot
  update `final_key` (column grants, tested).
- `check_stem` forces `aweme_id` to be numeric, so the key folder cannot be steered.

## Plan follow-ups

All Todo items are implemented: the handler tests, the handler with claim, its own thread and
temp-file cleanup, and the env/pm2 config. Every test scenario in the phase file is present. Before
marking the phase done, fix H1 and M1, and add the M1 test.

## Unresolved questions

- Does `pm2 reload --update-env` drop `REVIEW_WORKER_EXPORT_ROOT` when the ecosystem file stops
  setting it, or keep the old value?
- Should a missing root leave requests `pending` (L1)? This is a product decision.

Status: DONE_WITH_CONCERNS
Summary: The export path is sound: SKIP LOCKED claim, symlink-safe directory walk, atomic replace, no path leaks, and a genuine overlap test (confirmed by mutation). Two fixes are needed: a set-but-unmounted export root stops the whole review worker from starting (H1), and a directory named like a temp file blocks every export indefinitely (M1).

## Disposition (implementer, 2026-11-14)

| # | Outcome |
|---|---------|
| H1 | Fixed. `REVIEW_WORKER_EXPORT_ROOT` must only be absolute at start; a missing directory is logged as a warning and each export fails with "Thư mục gốc trên Mac không tồn tại." Red test first (`test_export_root_setting`). |
| M1 | Fixed. `clean_stale` catches `OSError` per entry, so a directory named like a temp file is left alone and no longer stops the drain. Red test first (`test_a_directory_named_like_a_temp_file_neither_goes_nor_blocks_exports`). A `S_ISREG` pre-check was tried and dropped: mutation showed it adds nothing beyond the per-entry catch (unlinking a directory fails on macOS and Linux). Leading-dot segments stay allowed so the worker's rule matches the DB CHECK. |
| M2 | Not changed in phase 6. The variable reaches the worker through the pm2 env of the shell that runs `start-all.sh`; phase 8 sets it and documents where. |
| L1 | Kept: a missing root fails the request (product call; the user re-clicks). |
| L2 | Fixed: the shutdown comment now says the temp file is removed by the next drain after the lease runs out. |
| L3 | Not changed: the Mac runs `studio` only. |
| L4 | Fixed: the warning log carries the dashboard sentence (errno name or S3 code, never a path). |
| L5 | Added tests: 403 message, lost claim between MP4 and SRT, `stopping` ends the drain. |
| L6 | Fixed: the directory is fsynced after each rename. |

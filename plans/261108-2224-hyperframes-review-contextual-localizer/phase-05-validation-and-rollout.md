---
phase: 5
title: "Migration, auth, UI entry và rollout"
status: pending
priority: P1
effort: 3d
dependencies: [4]
---

# Phase 5: Migration, auth, UI entry và rollout

## Goal

Chứng minh upgrade/recovery, thêm discovery/UI trạng thái đồng nhất và bật project delivery cho run mới sau canary; không tự reprocess video cũ.

<!-- Updated: Red Team Review 2026-11-09 - atomic migration ledger, recorded delivery mode and complete status consumers -->

## Files sở hữu

Các file bên dưới dùng đường dẫn tuyệt đối; phần Steps/Verification dùng cwd đã ghi.

- Modify `/Users/macbook/Data/reup-flow/scripts/apply-migrations.sh`; create `/Users/macbook/Data/reup-flow/scripts/tests/apply-migrations.test.sh` — transactional DDL+ledger và bootstrap ledger theo bằng chứng schema, không seed mọi migration hiện có chỉ vì videos tồn tại.
- Modify `/Users/macbook/Data/reup-flow/scripts/e2e-smoke.sh`, `/Users/macbook/Data/reup-flow/scripts/bootstrap.sh`, `/Users/macbook/Data/reup-flow/docker-compose.yml`, `/Users/macbook/Data/reup-flow/infra/minio/init.sh` — mode rollout, bridge profile/identity/health/loopback; test `/Users/macbook/Data/reup-flow/services/downloader/test/compose-constraints.spec.ts`.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/config.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/starter/outbox_starter.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/starter/test_outbox_starter.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/test_migration.py`; create `/Users/macbook/Data/reup-flow/services/localization/tests/workflows/test_delivery_mode.py`.
- Create `/Users/macbook/Data/reup-flow/services/downloader/src/reviews/reviews.module.ts`, `/Users/macbook/Data/reup-flow/services/downloader/src/reviews/reviews.controller.ts`, `/Users/macbook/Data/reup-flow/services/downloader/src/reviews/reviews.repository.ts`, `/Users/macbook/Data/reup-flow/services/downloader/src/reviews/reviews.schemas.ts`; modify `/Users/macbook/Data/reup-flow/services/downloader/src/app.module.ts` và tests dưới `/Users/macbook/Data/reup-flow/services/downloader/test/reviews/`, `/Users/macbook/Data/reup-flow/services/downloader/test/auth/every-route-requires-api-key.e2e-spec.ts`, `/Users/macbook/Data/reup-flow/services/downloader/test/architecture/http-lane-boundary.spec.ts`.
- Modify `/Users/macbook/Data/reup-flow/services/downloader/src/workspace/workspace.schemas.ts`, `/Users/macbook/Data/reup-flow/services/downloader/src/workspace/workspace.service.ts`, `/Users/macbook/Data/reup-flow/services/downloader/src/workspace/workspace.catalog.ts` và tests tương ứng trong `/Users/macbook/Data/reup-flow/services/downloader/test/workspace/` — read-only terminal/status/activity catalog, không tạo bàn agent mới nếu chỉ là deterministic I/O.
- Modify `/Users/macbook/Data/reup-flow/services/web/src/App.tsx`, `/Users/macbook/Data/reup-flow/services/web/src/api/client.ts`, `/Users/macbook/Data/reup-flow/services/web/src/api/types.ts`, `/Users/macbook/Data/reup-flow/services/web/src/workspace/workspace.layout.ts`, `/Users/macbook/Data/reup-flow/services/web/src/workspace/RunProgress.tsx`, `/Users/macbook/Data/reup-flow/services/web/src/workspace/RunPanel.tsx` và tests status/drift; create `/Users/macbook/Data/reup-flow/services/web/src/reviews/ReviewProjectsPage.tsx` và test — discovery/local launch instructions, không chuyển Workspace thành editor.
- Modify `/Users/macbook/Data/reup-flow/README.md`; create `/Users/macbook/Data/reup-flow/docs/hyperframes-review.md`; cập nhật nhỏ `/Users/macbook/Data/reup-flow/docs/agent-workspace.md` khi status/catalog contract thay đổi.

## Migration protocol

1. Backup database/schema trước mọi thay đổi live; lưu vào ignored backups/, checksum và thử restore clone. Các lệnh test chỉ chạy trên clone/throwaway DB; không chạy script test truncate/drop vào production.
2. Thay logic ledger-less hiện tại ở `scripts/apply-migrations.sh:30`: chỉ đánh dấu migration có schema evidence cụ thể (table/column/constraint), hoặc baseline cố định đã kiểm chứng. Migration mới project_key thiếu column phải thực sự được áp dụng. Schema không nhận diện được thì báo cần reconcile, không giả vờ tất cả đã chạy.
3. Mỗi DDL migration và ledger insert commit trong **cùng transaction**. Chuẩn hóa runner-owned transaction, bỏ wrapper BEGIN/COMMIT trong file tương ứng nếu cần, hoặc file tự transaction phải ghi ledger trong chính transaction đó; runner từ chối file không thỏa contract. Giữ ledger cũ tương thích, không mở rộng thành framework migration mới.
4. Tests bắt buộc: fresh DB; DB có ledger; DB có tables nhưng thiếu ledger/column mới; crash sau DDL trước ledger; second run; down/restore trên clone. Chỉ migration project thêm nullable project_key; status hiện là text, không backfill/đổi status các run cũ.

## Discovery, quyền và launch

GET /api/reviews chỉ đọc aweme/generation/status/project HEAD locator từ DB, paginated và schema-validated, qua global X-Api-Key. Không đọc review bucket hoặc start/signal Temporal. Đây là query read-only; không tuyên bố DB principal cũ đã bị giảm quyền nếu source vẫn dùng role hiện có.

Review tab hiện ready_for_review/needs_human và hướng dẫn `node scripts/review-studio.mjs open <aweme> <generation>` trên cùng Mac. Link companion/Studio khi launcher đã chạy không mang long-lived key hoặc cấp quyền bootstrap. Bridge unavailable hiển thị rõ; web không điều khiển host Docker qua HTTP. Launcher Phase 4 chịu trách nhiệm one-time grant và PID.

MinIO identity bridge chỉ review Get/Put và scoped List, không Delete/source/work/cache/output. Provision credential server-side theo workflow quản lý secret hiện có khi triển khai, không commit secret hoặc đưa vào web bundle; test policy bằng đúng identity mới. Compose bridge không nhận provider/DB/LOC_S3/root secrets, không mount Docker socket. Bind trong container 0.0.0.0, host publish 127.0.0.1:3310; Studio host loopback 3002, test bằng socket và exact Origin chứ không chỉ URL in ra.

## Activation và status consumers

- Triển khai additive schema, worker có cả legacy/project branch và bridge trong khi `LOCALIZATION_DELIVERY_MODE=legacy_render` tại starter. Không bật blank-aware/no-render branch cho input legacy. Mode mặc định của input cũ luôn legacy để replay.
- Canary operator gửi **một workflow input mới** delivery_mode=review_project bằng Python CLI trong môi trường compose/starter, dùng generation/ID mới và cùng input validation/voice/source identity như starter; không mutate hàng outbox cũ, không flip default cho toàn bộ backlog để thử một video. Cung cấp CLI subcommand/tests trong starter-owned surface; không thêm Temporal writer vào downloader.
- Sau canary đủ acceptance mới đặt config starter `LOCALIZATION_DELIVERY_MODE=review_project`, tạo lại riêng localization-starter. Nếu fail, đổi về legacy_render và tạo lại starter; các run đã start giữ mode đã ghi. Dừng bridge nhận request mới nhưng giữ worker mới để hoàn tất histories/revoice jobs đang có; không rollback worker về binary không hiểu branch mới.
- Khép kín enum/type/labels: Python PublishOutcome/PublishRequest/Result + project_key; downloader GATE_OUTCOMES, service terminal events/labels và catalog activity; web GateOutcome/layout/RunProgress/RunPanel; smoke script terminal exit. ready_for_review không xuất hiện như published hoặc failed. needs_human kèm project vẫn cần human review; không trông chờ review_key MP4 cũ có giá trị.
- e2e-smoke mặc định mới thành công khi ready_for_review, output absent và HEAD/bundle hợp lệ; needs_human báo cần người sửa và exit riêng/nonzero theo contract được ghi rõ. Legacy verification có mode explicit để tiếp tục đòi published; không đổi nghĩa exit âm thầm.

## Verification (hẹp trước, rộng sau)

```bash
cd /Users/macbook/Data/reup-flow
bash scripts/tests/apply-migrations.test.sh
node --test scripts/tests/review-studio.test.mjs
pnpm --filter @reup-flow/downloader test -- --runInBand
pnpm --filter @reup-flow/downloader lint
pnpm --filter @reup-flow/downloader build
pnpm --filter @reup-flow/web test
pnpm --filter @reup-flow/web lint
pnpm --filter @reup-flow/web build
cd /Users/macbook/Data/reup-flow/services/localization
uv run pytest -q tests/starter/test_outbox_starter.py tests/workflows/test_delivery_mode.py tests/test_migration.py tests/replay
uv run pytest -q
uv run ruff check src tests
```

Chỉ broaden/repeat khi shared failure chưa giải quyết; không chạy lại cùng suite thêm ở root nếu kết quả trên đã đủ. Test DB host/port đúng môi trường (Mac hiện dùng 5433), layout dùng runtime có raqm. Live Studio/provider/MinIO integration là kiểm chứng riêng, không thay bằng test stub. Ghi explicit prerequisites/skip và không tuyên bố feature pass nếu live gate chưa chạy.

## Acceptance / rollback

- [x] Backup/restore, ledger-less/fresh/existing upgrade và crash atomicity chứng minh trước live apply.
- [x] Hai generation interleave không trộn asset; conditional snapshot + cold-open HEAD + resume lost response pass.
- [ ] Review tab/Workspace/API/smoke đồng nhất hai terminal mới; downloader vẫn read-only, bridge chỉ có quyền tối thiểu.
- [ ] Single-context inferred/unresolved, all-blank, real Studio style/SRT persistence và one-cue TTS thực tế pass; không render cuối/output/auto publish.
- [ ] Canary/flip default/rollback đang có run project được test; legacy/Vbee replay/build/lint/tests pass hoặc lỗi prerequisite còn lại được nêu rõ, không giấu.

Rollback thứ tự: chuyển starter default về legacy và recreate riêng starter; ngừng request bridge mới; giữ worker tương thích cho run đã ghi mode; dừng Studio đúng managed PID; revert API/web nếu cần khi schema additive vẫn còn. HEAD rollback bằng CAS về manifest cũ; không chỉ sửa local folder. DB down/restore chỉ sau backup, không còn consumer mới và đã có quyết định vận hành cần thiết. Không xóa snapshot/history hoặc tự chạy lại video cũ.

## Unresolved questions

2026-11-10 follow-up: guarded launcher now opens and plays the full generation-2
canary with a populated 03:09 timeline. [Evidence](../reports/ops-261110-1916-native-studio-full-video.md).
Default activation and remaining round-trip acceptance stay open.

Không còn quyết định sản phẩm. Studio pin/adapter, CAS backend, migration clone, live provider và raqm là technical gates phải chứng minh khi triển khai, chưa phải kết quả đã pass.

## Execution evidence — 2026-11-10

Implementation and isolated migration/API/UI/canary/policy checks are present.
[Execution report](../reports/cook-261110-0658-hyperframes-phase-05.md) records verification
and limits. No production migration, provider fixture, canary or default flip ran.
The native launcher was exercised under Node 24 with an empty scratch project and
failed closed at the Studio Host/Origin gate before materializing review data.
Production was checked read-only: starter is still `legacy_render`, and `project_key`
is absent. Acceptance remains open for the native Studio/live-provider and deployed
identity gates; local/isolated checks do not authorize activation.

Final regression evidence: starter/mode/migration/replay 449 passed; architecture,
manifest, generation and bridge 84 passed; Linux affected regression 126 passed;
API/auth/Workspace focused 140 passed; web 96 passed; exact isolated MinIO policy
passed. Full Linux initially had 3,384 pass / 4 fail; all four repaired paths passed
the 126-test recheck. Full downloader had 871 pass / 1 shutdown failure; that unchanged
file passed its 25-test recheck. Full runs and focused rechecks remain distinguished
in the report. No assertion, import guard or acceptance gate was disabled.

## Subsequent explicit live canary — 2026-11-10 afternoon

The user explicitly requested a new generation of the latest video after verifying
the local patched multilingual runtime and `ngoc-huyen`. See [live execution evidence](../reports/ops-261110-1523-voicestudio-review-canary.md).
The additive production migration was applied after backup/clone restore, compatible
workers/starter were deployed, and generation 2 completed as `needs_human` with a
checksum-verified project, 6 inferred cues and no final output. Backend revoice passed
with cached clips; a separate real TTS warmup proved provider inference. Default
dispatch remains legacy. This one-canary authorization does not accept the still-failing
native Studio gate or prove its live editor/button persistence; broad activation stays open.

Subsequent user-directed fixes: generation 3 now grounds Localizer requests in
timestamped source OCR and completed as `needs_human`, with no render. Native Studio
playback, fixed 20px captions, continuous white box at 25% alpha and 60% baseline
width are verified. TTS HTTP deadline is now separate from STT/OCR. See
[current execution evidence](../reports/fix-261110-2003-source-grounded-localizer.md).
This supersedes the earlier native-boundary blocker; remaining round-trip/rollout
acceptance stays open.

## Explicit local activation — 2026-11-10 evening

The user subsequently requested project delivery for extension downloads and automatic
local Studio opening for both `ready_for_review` and `needs_human`. This authorizes the
current Mac activation independently of the incomplete broader acceptance checklist.
The Compose override now pins `review_project` for newly dispatched runs; recorded
inputs retain their original mode. A host LaunchAgent polls read-only completed
project identities and uses the guarded launcher, with persistent deduplication and
queueing while another project is open. See [local auto-open operations](../../docs/review-companion.md#tự-mở-trên-mac-hiện-tại).
Remaining native/provider acceptance items above have not been marked passed.

---
title: "Bù backend cho phần thiết kế Stitch còn thiếu"
description: "Làm các phần của thiết kế dashboard đã bị bỏ khi restyle vì API chưa có dữ liệu: thumbnail, bảng cue, ước tính, thống kê máy host, tunnel, ưu tiên hàng đợi và các trang sidebar."
status: pending
priority: P2
effort: 46h
branch: feat/localization-dubber-code-mode
tags: [feature, frontend, backend, database]
blockedBy: []
blocks: []
created: 2026-11-12
---

# Bù backend cho phần thiết kế Stitch còn thiếu

## Overview

Bản restyle theo Stitch (journal `plans/journals/2026-11-12-cook-admin-dashboard-stitch-restyle.md`)
chỉ giữ những phần có dữ liệu thật. Plan này làm nốt các phần đã bị bỏ, mỗi hạng mục có nguồn
dữ liệu thật, hoặc được đánh dấu rõ là xung đột ràng buộc để bạn quyết định.

Thiết kế gốc: `plans/261111-1644-admin-dashboard/design-stitch/{dashboard-overview,video-detail}.html`.
Hiện trạng: `docs/admin-dashboard.md`. Plan trước: `plans/261111-1644-admin-dashboard/`.

## Ràng buộc kế thừa (không đổi)

Một người dùng chạy local. Downloader giữ Temporal **chỉ đọc** (không hủy, không chạy lại từng
bước). Mọi cổng host bind `127.0.0.1`; qua tunnel chỉ trỏ tới 5173. Media qua link ký. Không có
mock data trong UI: thiếu nguồn thì ẩn phần tử, không bịa số. Nguồn không đọc được hiện `unknown`,
không hiện như sự thật.

## Bảng 11 hạng mục

| # | Hạng mục thiết kế | Nguồn dữ liệu thật | Cần backend? | Phase | Trạng thái so với ràng buộc |
|---|---|---|---|---|---|
| 1 | Thumbnail video | khung hình cắt từ MP4 nguồn trên máy host, lưu ở MinIO `output/thumbs/` (ảnh bìa Douyin có trong `aweme_list` nhưng **không lưu**, không dùng, D1) | Có (worker + media) | 5 | OK (D1 đã chốt) |
| 2a | Bảng cue phụ đề / đối soát | `ReviewCue` trong project review, lưu vào cột `cues` của `localization.review_status` (cùng câu ghi với `revision`) | Có | 4 | OK |
| 2b | Bộ chuyển generation | `generations[]` đã có trong chi tiết video; cue và media đã nhận tham số generation | Một phần | 4 | OK |
| 3 | Ước tính chi phí LLM và thời gian render | `llm_calls.cost_usd` (đã có), thời gian render (chưa lưu) | Có (1 cột) | 3 | OK, ghi rõ là ước tính |
| 4 | Thống kê máy host | heartbeat của worker review (`host_workers.info`), thêm CPU/RAM/load | Có (worker) | 2 | OK; tên máy cần quyết định: **D5** |
| 5 | Chỉ báo CF Tunnel | probe `cloudflared` từ worker trên máy | Có (worker) | 2 | OK; cloudflared chạy ngoài repo nên cần cấu hình: **D6** |
| 6 | Đếm ngược tự làm mới | khoảng poll và thời điểm lần cập nhật cuối đã có ở client | Không | 1 | OK |
| 7 | Chu kỳ đồng bộ từng kênh | không có bộ lập lịch quét kênh; chỉ có `profiles.last_synced_at` (UI đã hiện) | Không khả thi | — | **Xung đột phạm vi** (tự quét kênh nằm ngoài phạm vi): **D2** |
| 8 | Ô "Ưu tiên hàng đợi" | thêm cột ưu tiên vào outbox; starter đang xếp theo `next_attempt_at` | Có | 6 | OK; phạm vi hàng đợi: **D3** |
| 9 | Thanh phím tắt | client | Không | 1 | OK; phím duyệt chỉ mở hộp xác nhận |
| 10 | Badge Localhost/Tunnel, Single Admin | `location.hostname` của client | Không | 1 | OK |
| 11a | Sidebar: Trình đối soát Media | `review_status` + cue (phase 4) | Một phần | 7 | OK |
| 11b | Sidebar: Node & Cụm Worker | poller của 7 hàng đợi Temporal, heartbeat worker, thống kê máy (phase 2); mở rộng `/health`, không thêm route mới | Có (đọc) | 7 | OK |
| 11c | Sidebar: Luồng & Workflow + Nhật ký Temporal | workflow mở và lịch sử activity (reader Temporal đã có, chỉ đọc) | Có (đọc) | 7 | OK; gộp hai mục: **D4** |
| 11d | Sidebar: Cài đặt | cấu hình không bí mật (stuck minutes, TTL link media, giới hạn hàng loạt…) | Có (đọc) | 7 | Chỉ đọc; sửa được thì xung đột "không UI sửa env": **D4** |
| 11e | Nút Tải lại worker | cần chạy `pm2 restart` từ dashboard | Có | — | **Xung đột**: mở rộng quyền điều khiển máy host từ web: **D7** (khuyến nghị bỏ) |

## Quyết định cần bạn chốt

Mỗi quyết định có một khuyến nghị mặc định. Plan được viết theo khuyến nghị; đổi quyết định thì phase liên quan phải sửa.

| ID | Câu hỏi | Khuyến nghị | Ảnh hưởng nếu đổi |
|---|---|---|---|
| D1 | Thumbnail lấy từ đâu: (a) ảnh bìa Douyin lưu lúc tải, (b) cắt khung hình từ MP4 nguồn bằng ffmpeg trên máy host | (b). Ảnh bìa Douyin cần thêm request tới CDN Douyin (plan trước đã cố ý giảm số request để khỏi bị rate-limit, `videos.play_addr` ghi chú F1); bản (b) chỉ đọc file đã nằm trong MinIO | Phase 5 |
| D2 | Chu kỳ đồng bộ kênh: (a) chỉ hiện "đồng bộ lần cuối" (đã có), (b) làm bộ quét kênh tự động | (a). (b) là tính năng mới đã nằm ngoài phạm vi; nếu muốn thì lập plan riêng | Hạng mục 7 giữ nguyên là "không làm" |
| D3 | "Ưu tiên hàng đợi" áp cho (a) chỉ hàng đợi localization (outbox), (b) cả hàng đợi tải của downloader | (a). Tải đã nhanh, nghẽn nằm ở localization (giới hạn số run đồng thời trong starter) | Phase 6 |
| D4 | Sidebar: (a) gộp "Luồng & Workflow" và "Nhật ký Temporal" thành một trang, "Cài đặt" chỉ đọc; (b) tách riêng, Cài đặt sửa được | (a). Sửa cấu hình từ web cần kho cấu hình mới và đụng bí mật trong `.env` | Phase 7 |
| D5 | Hiện tên máy trong thống kê host | Không hiện hostname; hiện model chip và RAM. Qua tunnel, hostname là thông tin thừa | Phase 2 |
| D6 | Phát hiện tunnel: cloudflared chạy ngoài repo (`scripts/` và `ecosystem.config.cjs` không có cloudflared) | Worker probe `CLOUDFLARED_METRICS_URL` (endpoint `/ready` của cloudflared); chưa đặt biến thì **ẩn badge Tunnel** (đã chốt khi validate) | Phase 2 |
| D7 | Nút Tải lại worker | **Bỏ.** Làm được qua bảng yêu cầu như `studio_open_requests`, nhưng khởi động lại worker giữa render/dub làm mất việc đang chạy, và đây là đường điều khiển tiến trình máy host từ web | Hạng mục 11e giữ là "không làm" |

## Phases

| # | Phase | Status | Phụ thuộc | Effort |
|---|-------|--------|-----------|--------|
| 1 | [Phần chỉ cần giao diện](./phase-01-ui-only-polish.md) | Pending | — | 3h |
| 2 | [Thống kê máy host và tunnel](./phase-02-host-and-tunnel-telemetry.md) | Pending | — | 6h |
| 3 | [Ước tính chi phí và thời gian render](./phase-03-bulk-estimates.md) | Pending | — | 5h |
| 4 | [Bảng cue và chuyển generation](./phase-04-cue-reconciliation.md) | Pending | 3 | 10h |
| 5 | [Thumbnail video](./phase-05-video-thumbnails.md) | Pending | 4 | 7h |
| 6 | [Ưu tiên hàng đợi localization](./phase-06-queue-priority.md) | Pending | 5 | 5h |
| 7 | [Các trang sidebar](./phase-07-sidebar-pages.md) | Pending | 2, 4 | 7h |
| 8 | [Docs và kiểm thử đầu-cuối](./phase-08-docs-and-end-to-end-verification.md) | Pending | 1-7 | 3h |

**Thứ tự thực hiện:** phase 3 → 4 → 5 → 6 chạy **tuần tự** (cùng sửa `dashboard.repository.ts`, `dashboard.schemas.ts`, `services/web/src/api/types.ts`, `PipelineTable.tsx`, `VideoDetail.tsx` và các bảng đăng ký migration). Phase 1 và 2 độc lập với chuỗi này và với nhau (phase 1 chạm `AppShell.tsx`, `DashboardPage.tsx`, `PipelineTable.tsx`; phase 2 chạm `HealthBar.tsx`, `AppShell.tsx`, `dashboard.health.ts`: nếu chạy song song, phase 2 hợp nhất sau cùng ở `AppShell.tsx`). Phase 7 cần số liệu máy host (2) và dữ liệu cue (4).

## Success Criteria

- [ ] Mỗi hạng mục 1-6, 8-10, 11a-11d hiện đúng từ dữ liệu thật trên stack thật; hạng mục 7 và 11e được ghi "không làm" kèm lý do trong `docs/admin-dashboard.md`.
- [ ] Không phần tử nào của thiết kế hiện số liệu mẫu; thiếu nguồn thì ẩn hoặc hiện `unknown`.
- [ ] Trang chi tiết video hiện bảng cue của generation đang chọn; đổi generation đổi cả cue và media.
- [ ] Thanh hàng loạt hiện ước tính chi phí và thời gian render kèm số video mẫu, và ẩn khi mẫu ít hơn 3.
- [ ] Tắt worker review thì thống kê máy host và tunnel (nếu đã cấu hình) chuyển `unknown`, không giữ số cũ; chưa cấu hình tunnel thì badge ẩn.
- [ ] Sau khi có cột `priority` và grant, starter vẫn dispatch bình thường (kiểm bằng role `localization` thật và một video thật), và video ưu tiên được lấy trước.
- [ ] Heartbeat của worker vẫn đập đều (không quá 10 giây giữa hai lần ghi) khi luồng thumbnail và backfill cue đang chạy.
- [ ] Mọi route mới yêu cầu API key (test `every-route-requires-api-key`), mọi cổng vẫn bind loopback.
- [ ] `pnpm -r lint`, `pnpm -r test`, `pnpm -r build`, test Python của localization và `scripts/run-mutations.sh` không có lỗi mới so với trạng thái trước plan (các lỗi có từ trước: `compose-constraints`, thiếu `raqm` của Pillow, chín test Python, hardcode cổng 5432 của vài suite).

## Đăng ký migration

Tên file theo dạng `<yymmdd>-<slug>`; mỗi migration phải được đăng ký ở **mọi** điểm bên dưới (đã đối chiếu với code, kiểm lại tên chính xác lúc cook). Phase 5 không có migration.

| Phase | Migration | Thư mục | Đăng ký ở |
|---|---|---|---|
| 3 | `final-render-duration` (thêm `final_renders.render_ms`) | `services/localization/migrations/` | `scripts/apply-migrations.sh` (mảng thứ tự), `scripts/migration-evidence.sql`, `services/downloader/test/support/localization-db.ts`, `services/localization/tests/review_worker/conftest.py`, `scripts/tests/apply-migrations.test.sh` (cặp down/up mới nhất) |
| 4 | `review-status-cues` (thêm `review_status.cues jsonb`) | `services/localization/migrations/` | như trên |
| 6 | `outbox-priority` (cột `localization_outbox.priority`, `videos.localize_priority`) | `services/downloader/migrations/` | `scripts/apply-migrations.sh`, `scripts/migration-evidence.sql`, `services/downloader/test/support/db.ts`, `services/downloader/src/db/schema.ts`, `services/downloader/test/db/schema-migration.spec.ts` (`ROLLBACK_FILE` nếu là migration mới nhất), `services/localization/tests/support/scratch_db.py` |
| 6 | `outbox-priority-grant` (`GRANT SELECT (priority) ON localization_outbox TO localization`) | `services/localization/migrations/` | `scripts/apply-migrations.sh`, `scripts/migration-evidence.sql` (nhánh adopt trả `false`, giống mục grant hiện có), `services/downloader/test/support/localization-db.ts`, `services/localization/tests/support/scratch_db.py`, `services/localization/tests/starter/test_grants.py` |

Grant và owner luôn nằm trong migration **luôn được áp**, không nhét vào migration DDL mà nhánh adopt của `migration-evidence.sql` có thể bỏ qua. `localization.review_status` và `final_renders` đã thuộc `localization`, nên phase 3 và 4 không cần đổi owner.

## Rủi ro chung

- **Migration:** làm tuần tự theo bảng ở trên và theo thứ tự phase (3, 4, 6). Sao lưu DB trước khi áp (quy tắc chung). Mỗi migration có `.down.sql`; kiểm down rồi up lại bằng `scripts/tests/apply-migrations.test.sh`.
- **Quyền DB:** **không có role Postgres tên `downloader`**: downloader chạy bằng superuser `reup` (`infra/postgres/create-databases.sh:45-47`) nên đọc được `localization.*` mà không cần grant. Các tiến trình bị giới hạn là **starter và worker review chạy bằng role `localization` (NOSUPERUSER)**, chỉ đọc được các cột đã cấp (ví dụ `videos(aweme_id, sec_user_id, storage_path)` và năm cột của `localization_outbox`, `261026-outbox-starter-grants.sql:15-17`). Mọi truy vấn mới của hai tiến trình này phải dùng cột đã được cấp hoặc kèm migration grant, và **được test bằng đúng role `localization`**, không bằng superuser. Lỗi kiểu này không hiện thành lỗi dashboard: starter nuốt `psycopg.Error` và lặp, nên dispatch ngừng trong im lặng.
- **Thứ tự triển khai và rollback:** migration (cột, grant) → dựng lại image `nest`, `web`, `review-bridge`, `localization-starter` → reload worker pm2. Rollback ngược lại: hoàn tác mã trước rồi mới chạy `.down.sql`. Truy vấn `SELECT_VIDEOS` dùng chung nên mọi nguồn mới (ước tính, v.v.) là nguồn `attempt()` riêng và không được làm hỏng danh sách khi thiếu cột hoặc bảng.
- **Heartbeat của worker:** quét và heartbeat chạy trên cùng luồng chính; việc nặng (thumbnail, backfill) chạy luồng riêng hoặc theo lô nhỏ, và probe chậm được cache 30-60 giây.
- **Stack cũ:** container `nest`, `web`, `review-bridge` và `localization-starter` phải được dựng lại sau thay đổi; phase 8 gom việc này.
- **Qua tunnel:** `VITE_API_KEY` nằm trong bundle; mọi route mới chỉ đọc hoặc ký link, đều qua `ApiKeyGuard`, và không route nào nhận id workflow hay đường dẫn đối tượng từ client.

## Chưa xác minh (phase tương ứng phải kiểm trước khi làm)

- Image downloader **không có ffmpeg** (`services/downloader/Dockerfile`); phase 5 cắt khung hình ở máy host. Đường dẫn `ffmpeg` có tới được môi trường pm2 của worker không: `hostEnv` rơi về `PATH=/usr/bin:/bin:/usr/sbin:/sbin` (`scripts/lib/host-voice-env.mjs:21`), nên spike phase 5 phải kiểm và truyền `FFMPEG_BIN` tuyệt đối.
- Văn bản gốc tiếng Trung của từng cue nằm ở đâu: `ReviewCue` chỉ có `source_segment_ids`; phase 4 phải tìm transcript trong manifest hoặc project để nối (nếu không có thì bảng cue chỉ hiện cột tiếng Việt).
- `psutil` không có trong `services/localization/pyproject.toml`; phase 2 dùng `os.getloadavg`, `vm_stat` và `sysctl` của macOS.
- Cloudflared có bật cổng metrics (`--metrics 127.0.0.1:<cổng>`) trên máy này không; chưa đặt `CLOUDFLARED_METRICS_URL` thì badge Tunnel ẩn (đã chốt khi validate), còn đã đặt mà cloudflared không trả `/ready` thì badge hiện `down`.

## Red Team Review

### Session — 2026-11-12
**Findings:** 13 (13 accepted, 0 rejected)
**Severity breakdown:** 1 Critical, 8 High, 4 Medium
**Reviewers:** Security Adversary, Failure Mode Analyst, Assumption Destroyer, Scope & Complexity Critic (bốn bản gộp, đã loại trùng; toàn bộ có bằng chứng `file:line`). Người dùng chọn áp dụng cả 13.

| # | Finding | Severity | Disposition | Applied To |
|---|---------|----------|-------------|------------|
| 1 | `ORDER BY priority` làm role `localization` của starter lỗi `permission denied`, dispatch ngừng trong im lặng | Critical | Accept | Phase 6, 8, plan.md |
| 2 | Mô hình quyền DB sai (không có role `downloader`); thiếu grant cột và `OWNER` cho role `localization` | High | Accept | Phase 3, 4, 5, plan.md |
| 3 | Cờ ưu tiên của link đơn mất ở `markDone`; rerun không đặt lại cờ | High | Accept | Phase 6 |
| 4 | `thumb` phá hợp đồng link ký (regex trong `media-token.ts`, `resolveMediaKey` rơi về `final_key`, thiếu endpoint lô, header cache) | High | Accept | Phase 5 |
| 5 | Lưu ảnh ở Postgres dựa trên tiền đề sai; ffmpeg chạy trên file lạ thiếu giới hạn giao thức và định dạng | High | Accept (lưu MinIO `output/thumbs/`, tải về file tạm, whitelist) | Phase 5 |
| 6 | Quét chạy trên luồng heartbeat; thumbnail và probe mới làm heartbeat quá hạn; quy tắc RAM macOS luôn `degraded` | High | Accept | Phase 2, 5 |
| 7 | `CLOUDFLARED_METRICS_URL` đặt trong pm2 không tới được worker; kiểm loopback chỉ nằm trong Rủi ro | High | Accept | Phase 2 |
| 8 | Danh sách đăng ký migration sai và thiếu; câu "phase 1-6 song song" sai | Medium | Accept | plan.md (bảng đăng ký, thứ tự phase), phase 3, 4, 6 |
| 9 | `review_cues` song song với `review_status`, ghi không nguyên tử, generation cũ trả 404 | High | Accept (cột `cues` trên `review_status`, backfill) | Phase 4 |
| 10 | Ước tính render lệch (không chuẩn hóa độ dài, bỏ backlog); mẫu chi phí lẫn generation lỗi | Medium | Accept | Phase 3 |
| 11 | `SELECT_VIDEOS` dùng chung: thiếu cột là cả dashboard "Postgres down"; phase 8 không dựng lại starter; thiếu thứ tự deploy và rollback | High | Accept | Phase 3, 6, 8, plan.md |
| 12 | Trang Workflows: thiếu lọc loại workflow, lịch sử từng dòng (N+1, đẩy cache), id từ client | Medium | Accept | Phase 7 |
| 13 | Trang Cài đặt lộ bí mật qua denylist và nhãn env sai; route `workers` lặp `health`; xếp hạng review queue chỉ một trang | Medium | Accept | Phase 7 |

Hai điểm reviewer mâu thuẫn đã được kiểm lại bằng grep: `cueCount` **đã có** trong danh sách video (`dashboard.repository.ts:206`, `services/web/src/api/types.ts:223`), nên bước thêm `cue_count` ở phase 7 bị bỏ; và đề xuất để ffmpeg đọc qua URL ký bị thay bằng tải file tạm cộng whitelist (phát hiện 5).

### Whole-Plan Consistency Sweep
- Files reread: plan.md, phase-01 … phase-08.
- Decision deltas checked: 10 (thumbnail ở MinIO, không migration, không `has_thumb`; cue ở `review_status.cues`; ưu tiên qua `videos.localize_priority` cộng migration grant; phase 3→6 tuần tự; không có route `/workers`; ước tính chuẩn hóa theo độ dài và là nguồn riêng; Settings allowlist có nhãn nguồn; đường biến môi trường của worker; thứ tự deploy cộng dựng lại starter; effort 41h → 46h).
- Reconciled stale references: 8 (hàng thumbnail, hàng cue, hàng Workers, bảng phase và effort, câu "song song", mục Quyền DB, mục Chưa xác minh, danh sách file của phase 3-8); grep lại các từ khóa cũ (`video_thumbs`, `review_cues`, `has_thumb`, `bytea`, role `downloader`, `/api/dashboard/workers`) chỉ còn xuất hiện trong phần lý do và nhật ký Red Team.
- Unresolved contradictions: 0.

## Validation Log

### Session 1 — 2026-11-12
**Trigger:** `/ak-plan validate` sau red-team.
**Questions asked:** 5

#### Verification Results
- Tier: Full (guard: đã có `## Red Team Review` với bằng chứng `file:line` từ bốn reviewer, nên chỉ kiểm lại phần còn thẻ chưa xác minh)
- Claims checked: 0 mới (không còn thẻ `[UNVERIFIED]`; mục "Chưa xác minh" là các việc spike của từng phase, không phải claim về code đã có)
- Verified: 0 | Failed: 0 | Unverified: 4 (ffmpeg trong PATH của pm2, nguồn văn bản gốc của cue, cloudflared metrics, thứ tự `aweme_id` theo thời gian)

#### Questions & Answers

1. **[Scope]** Phase 4 (bảng cue): worker chỉ lưu cue của revision HEAD mà lần quét cuối thấy. Nếu admin đã duyệt một revision rồi HEAD đi tiếp (sửa trong Studio), cue của revision đã duyệt sẽ không còn xem được trong dashboard. Chấp nhận không?
   - Options: Chỉ HEAD, UI ghi rõ revision (Recommended) | Lưu cả cue của revision đã duyệt
   - **Answer:** Chỉ HEAD, UI ghi rõ revision
   - **Rationale:** Giữ phase 4 gọn; đủ cho đối soát trước khi duyệt.
2. **[Assumptions]** Phase 6 (ưu tiên): khi admin bấm Chạy lại một video đã từng được ưu tiên, cờ ưu tiên xử lý thế nào?
   - Options: Đặt lại về 0 trừ khi chọn lại (Recommended) | Giữ ưu tiên cũ
   - **Answer:** Đặt lại về 0 trừ khi chọn lại
   - **Rationale:** Tránh video từng ưu tiên chen hàng mãi.
3. **[Tradeoffs]** Phase 5 (thumbnail): làm cho video cũ thế nào khi lần đầu worker thấy hàng trăm video chưa có ảnh?
   - Options: Chỉ video đang hiển thị trên dashboard, một video mỗi lượt (Recommended) | Làm hết video cũ ngay khi khởi động | Chỉ video mới từ nay
   - **Answer:** Chỉ video đang hiển thị trên dashboard, một video mỗi lượt
   - **Rationale:** Tránh bùng nổ băng thông và CPU trong lúc render. Worker không biết trang nào đang mở, nên triển khai là xấp xỉ: mới nhất trước, một video mỗi lượt.
4. **[Risks]** Phase 2 (tunnel): chỉ báo tunnel cần cloudflared bật cổng metrics. Hiện repo không chạy cloudflared. Muốn thế nào?
   - Options: Tôi sẽ bật --metrics cho cloudflared (Recommended) | Chưa chạy tunnel, ẩn chỉ báo cho đến khi cấu hình
   - **Answer:** Chưa chạy tunnel, ẩn chỉ báo cho đến khi cấu hình
   - **Rationale:** Không hiện `unknown` gây hiểu lầm khi chưa có tunnel.
5. **[Scope]** Phase 7 (Luồng & Workflow): workflow đã kết thúc hiện bao lâu và bao nhiêu?
   - Options: 7 ngày gần nhất, tối đa 50 dòng (Recommended) | 24 giờ, tối đa 50 | 30 ngày, tối đa 200
   - **Answer:** 7 ngày gần nhất, tối đa 50 dòng
   - **Rationale:** Đủ truy lại lỗi trong tuần, truy vấn nhẹ trên Postgres dùng chung.

#### Confirmed Decisions
- Cue: chỉ revision HEAD, UI ghi rõ revision.
- Ưu tiên: đặt lại về 0 khi chạy lại, trừ khi chọn lại.
- Thumbnail video cũ: làm dần, mới nhất trước, một video mỗi lượt, không có đợt làm hàng loạt.
- Tunnel: chưa cấu hình thì ẩn badge (worker ghi `configured: false`; heartbeat cũ vẫn là `unknown`).
- Workflow đã đóng: cửa sổ 7 ngày, tối đa 50 dòng.

#### Impact on Phases
- Phase 2: khóa `tunnel.configured`, API trả `tunnel: null`, UI ẩn badge, test.
- Phase 4: ghi nhận chỉ lưu HEAD (không đổi thiết kế).
- Phase 5: thứ tự ứng viên mới nhất trước, một video mỗi lượt, không backfill hàng loạt.
- Phase 6: ghi nhận đặt lại ưu tiên khi chạy lại (không đổi thiết kế).
- Phase 7: cửa sổ 7 ngày và trần 50 dòng cho workflow đã đóng.
- Phase 8: docs ghi badge Tunnel ẩn khi chưa cấu hình.

### Whole-Plan Consistency Sweep
- Files reread: plan.md, phase-01 … phase-08.
- Decision deltas checked: 5.
- Reconciled stale references: 3 (hàng D6 trong bảng quyết định, phần UI và test của phase 2, danh sách docs ở phase 8).
- Unresolved contradictions: 0.

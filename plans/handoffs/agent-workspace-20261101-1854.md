---
handoff-version: 1
generated: 2026-11-01T11:54:00Z
generator: ak:handoff
focus: "phần workspace (plans/261031-1406-agent-workspace)"
workspace: /Users/vietphung/Data/reup-flow
branch: feat/localization-dubber-code-mode
head: 7466fee
---

# HANDOFF: Agent Workspace (tab văn phòng 2D của các agent localization)

## Mission and current status

Focus: "phần workspace (plans/261031-1406-agent-workspace)"

Outcome: tab "Workspace" trong `services/web` hiển thị một văn phòng 2D (SVG) cho thấy agent localization nào đang làm video nào, dựa trên dữ liệu thật qua một endpoint chỉ-đọc của `services/downloader`. Chỉ sự kiện thật mới tạo hiệu ứng. Người dùng yêu cầu báo cáo bằng tiếng Việt.

Done:
- Backend chỉ-đọc: `GET /api/workspace/snapshot` và `GET /api/workspace/runs/:awemeId/:generation`. Nguồn: Temporal (workflow đang mở, pending activities, history), `localization.llm_calls`, `localization.runs`, `localization_outbox`.
- Web: tab Jobs/Workspace (hash `#workspace`), SVG văn phòng, bong bóng thoại, mũi tên bàn giao có nhãn, màu theo video, panel chi tiết run, log sự kiện.
- Test: downloader 84 test workspace pass; web 87 test pass; lint và tsc sạch.
- Tài liệu: `docs/agent-workspace.md`. Plan `plans/261031-1406-agent-workspace/` đã đánh dấu completed.
- Đã commit `7466fee` (56 file), chưa push.
- Đã chạy thật vài video qua toàn luồng để xem trên UI; người dùng đã bảo dừng test và tắt container.

Remaining:
- Người dùng chưa xác nhận bằng mắt giao diện SVG mới và mũi tên mới (extension Chrome bị ngắt nên tôi không tự kiểm tra bằng trình duyệt được).
- Chưa kiểm tra trên workflow đang mở thật các trạng thái "working" và "retrying" (chỉ có test đơn vị).
- Câu hỏi thiết kế chưa trả lời: có ẩn các run `published` cũ ở cột "Khác" của danh sách cổng hay không (hiện hiển thị tối đa 50 run gần nhất).

Urgency: không gấp; chờ phản hồi của người dùng sau khi họ xem UI.

## Scope and guardrails

Workspace: `/Users/vietphung/Data/reup-flow` (monorepo pnpm; `services/downloader` NestJS 11, `services/web` React 18 + Vite + Tailwind).

In scope: mọi thứ dưới `services/downloader/src/workspace/**`, `services/downloader/src/temporal/**`, `services/web/src/workspace/**`, `docs/agent-workspace.md`, plan của feature.

Out of scope: công việc chưa commit khác của người dùng (extension, parser, jobs, videos, lanes, báo cáo plan bị xóa, submodule `src/Douyin_TikTok_Download_API`, `.DS_Store`).

Constraints (người dùng):
- Báo cáo bằng tiếng Việt.
- Conventional commit, không nhắc AI (nhưng nhắc nhở hệ thống hiện yêu cầu dòng `Co-Authored-By` cho commit mới; commit `7466fee` không có dòng này).
- Không commit secret/dotenv.
- Sao lưu trước mọi thay đổi dữ liệu hay schema.
- Chỉ dừng tiến trình do mình khởi chạy.
- Không lặng lẽ đảo ngược quyết định của người dùng.
- Dùng `rg -uu` khi tìm file bị gitignore.

Safety boundaries:
- Workspace chỉ đọc: không được gọi ghi vào Temporal hay Postgres. `.eslintrc.json` chỉ cho `@temporalio/*` trong `src/workspace/**` và `src/temporal/**`, và test kiến trúc kiểm tra không có lệnh ghi.
- Khi nguồn dữ liệu chết, UI phải hiện "không rõ" chứ không phải "rảnh".
- Hook `scout-block` chặn lệnh Bash nhắc tới thư mục `node_modules`; đừng tìm cách vòng qua.

## Current state

Branch: `feat/localization-dubber-code-mode`
HEAD: `7466feed2879a2bb3132674ce56433d045ae94ee` (`feat(workspace): read-only 2D office view of the localization agents`)
Working tree: bẩn, 56 mục chưa commit. Không có mục nào thuộc công việc workspace.

Changed files (không thuộc workspace, rút gọn): nhiều `plans/reports/*.md` bị xóa; sửa `services/downloader/src/{db/videos.repository.ts,jobs/*,lanes/enumerate.lane.ts,parser/parser.client.ts,videos/*}`; xóa `lanes/aweme.mapper.ts`; sửa test lanes và `test/mutations/phase-05.sh`; sửa `services/extension/*`; submodule `src/Douyin_TikTok_Download_API` ở trạng thái `m`.

Untracked files: `.DS_Store`, `services/downloader/src/parser/{aweme.mapper.ts,map-parser-error.ts}`, `services/extension/{content.js,lib/*.js,test/*.spec.js}` (các file mới của extension). Ngoài ra `backups/outbox-7665948504426075877-261031.csv` bị gitignore (bản sao lưu hàng outbox).

Intentional local modifications: yes (công việc khác của người dùng; không được stage hay hoàn nguyên).

Docker: toàn bộ container compose `reup-flow` đang tắt; chỉ còn các container `family-organizer-*` không liên quan. Video 7665948504426075877 (generation 8) và 7552039043057356090 (generation 3) có thể chưa xong trong Temporal và sẽ tiếp tục khi stack bật lại.

## Decisions and rationale

- Read-only qua Temporal client — Rationale: chỉ quan sát, không ảnh hưởng luồng thật — Alternative rejected: thêm bảng/sự kiện ghi riêng — Ref: `docs/agent-workspace.md`.
- Tên activity → agent bằng catalog regex có thứ tự; tên lạ vào `unknown` — Rationale: dữ liệu thật, không bịa agent — Ref: `plans/reports/spike-261031-1430-temporal-activity-names.md`, `workspace.catalog.ts`.
- Snapshot thuần (`buildSnapshot`), cache 2 s với promise dùng chung, timeout 3 s mỗi nguồn; nguồn chết → `down` nhưng vẫn trả 200 — Rationale: UI không vỡ khi Temporal chết.
- Bộ từ vựng sự kiện cố định: `activity_started`, `activity_retrying`, `activity_completed`, `activity_failed`, `llm_call_recorded`, `gate_decision`, `handoff`. Cửa sổ 10 phút, tối đa 50 sự kiện, `MAX_OPEN_WORKFLOWS` 10, bàn lỗi hiện 30 s, `HANDOFF_GAP_MS` 2000.
- Snapshot đầu tiên chỉ điền log, không phát lại hoạt ảnh; bong bóng khử trùng theo id sự kiện.
- Endpoint chi tiết trả 404 nếu run lạ, 503 nếu nguồn có thể biết run đang chết.
- Ước lượng GPU từ thời lượng queue `stt`/`tts` (gồm thời gian chờ queue).
- Kiểu dữ liệu web viết tay phản chiếu kiểu server; có test drift ở `services/downloader/test/workspace/web-layout.spec.ts`. Không import chéo gói trong web vì image web chỉ chứa `services/web`.
- UI: sau khi người dùng chê bản đầu là các ô vuông, đổi sang văn phòng SVG (viewBox 1000x660). Sau khi chê mũi tên rối, thêm màu theo video, mũi tên có nhãn "A → B · …4 số cuối", bong bóng gắn tag id video. Tôn trọng `prefers-reduced-motion`.
- Chạy lại video cũ = tăng `generation` và đặt hàng outbox về `pending` (starter gửi các hàng đến hạn; hàng đang đỗ có `next_attempt_at` 2026-11-30).

## Work performed

- Backend: `services/downloader/src/workspace/{workspace.catalog,workspace.schemas,workspace.temporal,workspace.repository,workspace.service,workspace.controller,workspace.module,workspace.tokens}.ts`; `src/temporal/temporal.module.ts` (global, kết nối lazy); `app.module.ts`, `config/env.ts` (`TEMPORAL_ADDRESS` mặc định `temporal:7233`, `TEMPORAL_NAMESPACE` mặc định `default`), `test/support/fixed-env.ts`, `docker-compose.yml` (env của `nest`), `.eslintrc.json`.
- Web: `services/web/src/workspace/` (`workspace.layout.ts`, `Desk.tsx`, `HandoffLine.tsx`, `OfficeMap.tsx`, `GateArea.tsx`, `RunPanel.tsx`, `TopBar.tsx`, `EventLog.tsx`, `Bubble.tsx`, `videoColor.ts`, `useEventFeed.ts`, `useWorkspaceSnapshot.ts`, `WorkspacePage.tsx`, `fixtures.ts`, test); `src/api/{types,client}.ts`, `src/hooks/usePolling.ts` (`pauseWhenHidden`), `src/App.tsx` (+test), `eslint.config.js`, `tailwind.config.js`.
- Docs/plan: `docs/agent-workspace.md`, `plans/261031-1406-agent-workspace/`, báo cáo spike.
- Chạy thật: nhả từng hàng outbox đang đỗ và tăng generation để chạy video; kiểm chứng endpoint live kể cả chế độ suy giảm; khớp chi phí với SQL. Đã sao lưu hàng outbox trước khi tăng generation.
- Sự cố đã gặp: cổng 3100 bị Langfuse chiếm (dùng 3127 cho container kiểm tra); lỗi tạm "remaining connection slots are reserved" khi Postgres gần 100 kết nối, sau đó tự hồi phục.
- 0 redactions applied.

## Verification

| Check | Command | Outcome | When |
| --- | --- | --- | --- |
| Test workspace downloader | chạy các spec trong `services/downloader/test/workspace/` | 84 pass | trước commit |
| Test web | chạy vitest trong `services/web` | 87 pass | trước commit |
| Lint và tsc | lint/tsc cả hai gói | sạch | trước commit |
| Kiểm tra đột biến (mutation) trên guard web | phá từng bất biến | các mutant sống đã được sửa | trong phiên |
| Endpoint live | gọi snapshot/detail trên stack thật | đúng, kể cả khi Temporal tắt | trong phiên |

Not run:
- Xác minh bằng mắt SVG văn phòng và mũi tên mới: extension Chrome bị ngắt.
- Đường pending-activity (working/retrying) trên workflow mở thật: chỉ có test đơn vị.
- Chạy lại toàn bộ suite sau cùng trong trạng thái tải thấp: có lần chạy song song đầy đủ làm timeout các suite bootstrap-failure, lane.supervisor, download-commit-race, health, redis; chạy riêng thì pass.

Known failure không thuộc workspace: `compose-constraints.spec.ts` fail vì allowlist env của `worker-agents` (`AD_GATE_*`, `VOICE_CHECK_*`, `TRACE_AGENT_CONTENT`) từ công việc trước của người dùng; không đụng tới `nest`, tôi chưa sửa.

## Open risks and blockers

- Type: question — Owner: người dùng — Impact: thấp. Có ẩn các run `published` cũ ở cột "Khác" của danh sách cổng không?
- Type: risk — Owner: unknown — Impact: giao diện SVG chưa được xem trong trình duyệt nên có thể còn lỗi bố cục.
- Type: risk — Owner: unknown — Impact: trạng thái working/retrying chưa được xác thực trên workflow mở thật.
- Type: risk — Owner: người dùng — Impact: `compose-constraints.spec.ts` đang đỏ, có thể làm CI đỏ dù không do workspace.
- Type: risk — Owner: unknown — Impact: commit chưa push; nhánh còn nhiều thay đổi khác chưa commit.

## Exact next actions

1. **First safe step** — Xác minh trạng thái: `git log -1 --oneline` phải là `7466fee`, và `docker compose ps` để xem stack đang tắt.
2. Chỉ khi người dùng muốn xem UI: `docker compose up -d` rồi mở `http://localhost:5173/#workspace`. Không khởi chạy lại video nếu họ chưa yêu cầu (hai video dang dở sẽ tự tiếp tục).
3. Thu phản hồi về hình dáng văn phòng và mũi tên; chỉnh trong `services/web/src/workspace/` (bố cục ở `workspace.layout.ts`) và chạy lại test web, lint, tsc.
4. Hỏi người dùng về câu hỏi cột "Khác" rồi áp dụng quyết định.
5. Nếu cần, xác thực working/retrying bằng một workflow mở thật khi stack đang chạy.
6. Chỉ push hoặc mở PR khi người dùng yêu cầu. Khi commit mới, chỉ stage file của workspace, không đụng tới công việc dở khác.

## Source pointers

- Commit: `7466fee`
- Plan: `plans/261031-1406-agent-workspace/`
- Spike: `plans/reports/spike-261031-1430-temporal-activity-names.md`
- Docs: `docs/agent-workspace.md`
- Backend: `services/downloader/src/workspace/`, `services/downloader/src/temporal/`, `services/downloader/test/workspace/`, `services/downloader/test/architecture/http-lane-boundary.spec.ts`
- Web: `services/web/src/workspace/`, `services/web/src/App.tsx`
- Sao lưu (gitignored): `backups/outbox-7665948504426075877-261031.csv`
- Transcript phiên trước: `/Users/vietphung/.claude/projects/-Users-vietphung-Data-reup-flow/f0f232bc-544f-40c8-a644-f31d9af8a8e2.jsonl`

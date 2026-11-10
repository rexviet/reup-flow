# Review giả định và contract: HyperFrames contextual localizer

Ngày review: 2026-11-09 (Asia/Ho_Chi_Minh)
Phạm vi: toàn bộ `plan.md`, 5 phase, brainstorm đã accepted, 2 báo cáo nghiên cứu và source/caller hiện hành. Không chạy lint/build/test; không sửa plan/code.

## Kết luận

**Chưa sẵn sàng thực thi nguyên trạng.** Đã kiểm tra **27 claim/contract** bằng source trực tiếp (contracts/exports, Localizer single + Temporal, các writer script/dub/audio/layout, workflow/publish, starter/worker queues, object-store keys, compose/bootstrap, downloader workspace và web status consumers). Có 3 lỗ hổng thực thi đáng kể dưới đây.

## Findings

### 1. Critical — Snapshot đích có generation nhưng artifact nguồn vẫn đè chéo generation

- **Vị trí plan:** `phase-03-review-project-and-workflow.md:19,34-36,41,52,64,68` tuyên bố prefix `review/{aweme}/g{generation}/r{revision}/`, mọi dependency được copy khỏi `work/`/`cache/`, và generation/revision ngăn run mới ghi đè.
- **Bằng chứng source:** `storage/object_store.py:23-27` tạo `work/{aweme}/r{round}/{name}` **không có generation**; `storage/object_store.py:34-35` tạo render key cũng không có generation. `activities/script_document.py:26-40` ghi script vào key đó; `activities/assemble_script.py:111-112`, `activities/dub_script.py:193-197` và `activities/assemble_audio.py:186-235` tiếp tục ghi/copy từ namespace chung theo aweme/round. `starter/outbox_starter.py:134-135,239-251` cho phép generation khác nhau của cùng aweme thành workflow/input riêng.
- **Failure cụ thể:** g1 và g2 cùng aweme chạy xen kẽ có thể ghi đè `script.vN.json`, `dub-segments.json`, `dubbed.wav` trước lúc g1 build bundle. Manifest g1 vẫn nằm trong prefix immutable của g1 và checksum nội bộ vẫn có thể hợp lệ, nhưng bytes thực tế đã đến từ g2. Destination generation-scoped không sửa được collision ở source.
- **Fix bắt buộc:** Phase 3 phải sở hữu migration đầy đủ của **mọi producer và consumer** sang work/render key có generation (không chỉ thêm key builder), hoặc tạo immutable generation snapshot ngay tại mỗi producer. Thêm test hai workflow cùng aweme/generation khác nhau, interleave write/build, rồi assert mọi manifest chỉ chứa provenance đúng generation. Giữ decoder/read branch cũ cho history Temporal đã ghi key legacy.

### 2. High — Topology bridge/Studio không thể hoạt động như mô tả

- **Vị trí plan:** `phase-04-studio-editing-and-line-voice.md:20,24-27,32,44-45` đặt bridge trong compose, yêu cầu server bind `127.0.0.1`, publish port loopback, đồng thời muốn bridge materialize/open project và host Studio CLI phục vụ cùng folder. Phase 5 (`phase-05-validation-and-rollout.md:19,35-36`) vẫn không giao owner/script host-side để start/stop Studio.
- **Bằng chứng source:** Docker publish chuyển traffic tới IP của container; repo hiện ghi rõ Temporal frontend phải bind container IP, không phải loopback (`docker-compose.yml:271-285`). Localization image chỉ chứa Python service (`services/localization/Dockerfile:24-28,48-55`), root Node workspace chưa có HyperFrames dependency/script (`package.json:9-23`), và bootstrap hiện chỉ quản lý compose services (`scripts/bootstrap.sh:116-140`), không có lifecycle/PID cho host Studio.
- **Failure cụ thể:** nếu bridge trong container thật sự listen `127.0.0.1`, mapping `127.0.0.1:HOST:CONTAINER` không tới được process. Kể cả sửa bind, container không thể tự spawn/track an toàn một CLI chạy trên macOS host; “Open Studio” và clean shutdown không có command, owner, resolved host path hay PID contract để thực thi acceptance.
- **Fix bắt buộc:** bridge listen `0.0.0.0` **bên trong container**, compose publish duy nhất `127.0.0.1:<host>:<container>` và test socket từ host + refusal qua non-loopback. Bổ sung host-side launcher được pin (script/CLI package + lockfile) nhận project path bind-mount đã canonicalize, quản lý PID/port/reuse/stop, và định nghĩa companion chỉ trả launch instruction/deep link hoặc gọi launcher qua một local authenticated boundary; container không được giả vờ spawn host process.

### 3. High — Canary/rollback không có delivery-mode điều khiển nhánh mới; status consumer scope cũng chưa khép kín

- **Vị trí plan:** `plan.md:47,59`, `phase-03-review-project-and-workflow.md:44,79` và `phase-05-validation-and-rollout.md:37-39,67-79` nói canary một run, enable sau khi inspect, rollback bằng “disable new dispatch”, còn history đã marker tiếp tục chạy. Không phase nào định nghĩa config/input/DB field nào chọn legacy hay review-project cho **run mới**.
- **Bằng chứng source:** `LocalizationInput` hiện không mang delivery mode (`contracts/models.py:20-26`); starter luôn build cùng shape (`starter/outbox_starter.py:239-251`) và chọn workflow chỉ theo provider pipeline (`starter/outbox_starter.py:43-47,161-165`). Trong workflow, `workflow.patched(...)` là compatibility marker/order gate và với execution mới sẽ đi nhánh mới; pattern hiện tại thể hiện tại `workflows/video_localization.py:337-370`. Vì vậy patch marker không phải feature flag có thể canary/disable. Ngoài ra status mới chưa nằm trong closed-set hiện hành: `downloader/src/workspace/workspace.schemas.ts:14-25`, `workspace.service.ts:46-51,212-232`, `web/src/api/types.ts:80-84`, `web/src/workspace/RunProgress.tsx:45-55`; Phase 5 Files chỉ nêu App/client/types/new Review page, không nêu các backend workspace consumers này (`phase-05-validation-and-rollout.md:20-24`).
- **Failure cụ thể:** deploy worker chứa marker mới sẽ chuyển mọi workflow mới sang project path; không thể canary đúng một run hoặc rollback bằng config như plan hứa. Nếu chỉ tắt starter, toàn bộ dispatch dừng chứ không quay run mới về legacy. Đồng thời `ready_for_review` không thành gate outcome/event/terminal label ở workspace hiện hữu, tạo hai cách hiểu trạng thái giữa Review tab và monitoring UI.
- **Fix bắt buộc:** thêm delivery mode explicit, immutable tại dispatch (ví dụ input/outbox field `legacy_render | review_project`, default legacy trong rollout), record mode trước branch và dùng patch marker chỉ bảo vệ replay. Chỉ flip default sau canary; rollback flip default về legacy trong khi workers vẫn đăng ký cả hai branch cho histories đang chạy. Mở rộng Phase 5 ownership/test cho downloader `GATE_OUTCOMES`/labels/service và web Gate/RunProgress/type consumers, đồng thời xác nhận `ready_for_review` và `needs_human` là terminal nhất quán trong API, smoke và UI.

## Claims đã xác minh không thành finding

- `LocalizedSegment` chưa có status/reason; `DubSegment.tts_key` đang bắt buộc (`contracts/models.py:147-169,214-230`).
- Initial single prompt có toàn transcript, repair hiện chỉ có dòng lỗi/glossary, blank hiện unusable (`agents/localizer_single.py:170-186,223-301`).
- Merge bỏ qua repair omission và giữ base; writer sau đó vẫn cần metadata-aware contract (`agents/localizer_single.py:304-319`).
- Temporal one-request hiện fail khi còn unusable (`agents/temporal.py:339-359`).
- `assemble_script`, synthesis/fit/commit/audio thực sự giả định mọi segment có text/clip; layout đã skip text rỗng nhưng fidelity check đòi mapping script↔dub (`activities/assemble_script.py:47-87`; `activities/dub_script.py:96-107,163-198`; `activities/assemble_audio.py:173-235`; `activities/layout_captions.py:261-282,393-405`).
- Vbee writer cũng tạo `LocalizedSegment`, nên default additive là cần thiết để không phá flow (`activities/slot_script.py:39-80`).
- `EditSpec`/`PublishResult.review_key` hiện đúng là render/MP4 contract cũ (`contracts/models.py:341-374,441-465`; `activities/publish.py:112-171`).
- Exporter hiện chỉ xuất schema `EditSpec`, nên plan thêm machine-readable manifest gate là đúng (`contracts/export.py:1-31`).
- Queue hiện hữu đúng là `tts`, `media`, `io`; activity revoice mới phải được đăng ký ở đúng worker/queue (`workers/run.py:121-155,230-290`).

## Câu hỏi chưa giải quyết

Không có câu hỏi sản phẩm mới. Ba finding là lỗ hổng kỹ thuật/rollout có thể sửa trong plan mà không đổi quyết định companion, ngưỡng 1,15× hay semantics unresolved đã được người dùng chốt.

Status: DONE_WITH_CONCERNS

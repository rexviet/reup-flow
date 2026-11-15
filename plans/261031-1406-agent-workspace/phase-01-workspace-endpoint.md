# Giai đoạn 1: Endpoint đọc-only `GET /api/workspace/snapshot`

Trạng thái: completed · Ưu tiên: P2 · Effort: 6h · Phụ thuộc: không

## Mục tiêu

Một endpoint trong `services/downloader` trả ảnh chụp trạng thái workspace: danh sách agent kèm trạng thái, các event gần đây, các run đang mở, và tình trạng từng nguồn dữ liệu.

## Việc cần làm

1. **Spike (không viết code sản phẩm):** khi người dùng có một video đang chạy, đọc lịch sử một workflow (qua Temporal UI hoặc `tctl`/`temporal` CLI trong container admin-tools) và ghi lại tên activity thật của từng agent LLM, queue của từng activity. Kết quả ghi vào `plans/reports/` rồi mới viết bảng ánh xạ. Nếu chưa có video đang chạy, dùng workflow đã hoàn tất của 8 mẫu hôm nay (vẫn còn trong lịch sử Temporal).
2. Thêm `@temporalio/client` vào `services/downloader/package.json`; tạo `TemporalModule` (`@Global()`, đọc `TEMPORAL_ADDRESS` và namespace từ config, theo mẫu `redis.module.ts`). Kết nối lười: downloader vẫn khởi động khi Temporal chưa sẵn sàng.
3. Migration cấp quyền: role của downloader được `SELECT` trên `localization.llm_calls`. Đặt cạnh các migration hiện có (`services/localization/migrations/`), kèm file `.down.sql`. Sao lưu `pg_dump` trước khi áp.
4. Tạo `services/downloader/src/workspace/` gồm:
   - `workspace.catalog.ts`: một bảng duy nhất ánh xạ tên activity → `{ agentId, zone }`. Activity không có trong bảng bị đưa vào nhóm `unknown` và hiện trong snapshot để không bị bỏ sót.
   - `workspace.repository.ts`: đọc `llm_calls` (N phút gần nhất) và `localization_outbox` (run đang mở, trạng thái cổng).
   - `workspace.temporal.ts`: liệt kê workflow đang mở, `describe` để lấy pending activities, lấy lịch sử ngắn của workflow mở để suy ra activity đã xong và `handoff`.
   - `workspace.service.ts`: ghép ba nguồn thành snapshot; bộ nhớ đệm khoảng 2 giây; mỗi nguồn lỗi thì đánh dấu `down` và tiếp tục với các nguồn còn lại.
   - `workspace.controller.ts`: `@Controller('api/workspace')`, `@Get('snapshot')`, chịu `ApiKeyGuard` toàn cục như mọi route.
   - `workspace.module.ts`, đăng ký vào `app.module.ts`.
5. Định nghĩa kiểu snapshot bằng zod trong `workspace.schemas.ts` (cùng phong cách `jobs.schemas.ts`) và xuất kiểu cho web dùng lại qua `services/web/src/api/types.ts`.

## Hình dạng phản hồi (dự kiến)

```ts
{
  generatedAt: string,                       // ISO, có múi giờ
  sources: { temporal: 'ok'|'down', postgres: 'ok'|'down' },
  agents: Array<{
    id: string, zone: 'media'|'voice'|'llm'|'gate',
    state: 'idle'|'working'|'retrying'|'failed',
    awemeId?: string, generation?: number, since?: string, attempt?: number
  }>,
  events: Array<{ at: string, kind: EventKind, agentId?: string, awemeId?: string, detail: string }>,
  runs: Array<{ awemeId: string, generation: number, phase: string, outcome?: GateOutcome,
                llmCostUsd: number }>
}
```

`detail` chỉ chứa mô tả ngắn do server dựng từ trường có cấu trúc (tên model, số token, loại lỗi). Không chuyển tiếp chuỗi tự do từ lỗi hay payload của Temporal.

## File sở hữu

Chỉ tạo mới dưới `services/downloader/src/workspace/`, `services/downloader/src/temporal/`, test tương ứng trong `services/downloader/test/`, một migration cấp quyền; sửa `services/downloader/package.json`, `pnpm-lock.yaml` và `services/downloader/src/app.module.ts`.

## Kiểm thử

- Đơn vị: bộ ghép snapshot với Temporal và Postgres giả; đủ các nhánh `ok`/`down`; activity lạ vào `unknown`; `detail` không chứa chuỗi tự do.
- Test kiến trúc hiện có phải vẫn xanh (module không import lanes).
- Tích hợp: dùng DB test sẵn có (`test/support/db.ts`) cho phần Postgres; phần Temporal dùng double có cùng hình dạng phản hồi gRPC mà code thực sự đọc (đọc từ kết quả spike, không đoán).
- Kiểm tay: gọi endpoint với `X-Api-Key` trên video thật, đối chiếu với Temporal UI.

## Nghiệm thu

- Endpoint trả đúng tên activity đang chạy trên một video thật (đối chiếu với Temporal UI).
- Tắt Temporal thì endpoint vẫn trả 200 với `sources.temporal = 'down'`; `/health` không đổi.
- Không có route ghi nào mới; không có nội dung prompt hay đoạn dịch trong phản hồi.
- `pnpm --filter downloader test`, lint và typecheck xanh.

## Rollback

Gỡ `WorkspaceModule` và `TemporalModule` khỏi `app.module.ts`, áp file `.down.sql` của migration quyền. Không có thay đổi dữ liệu nào cần hoàn.

---
title: "Phase 7: Giao diện collection trên dashboard"
status: completed
priority: P2
effort: "1d"
dependencies: [4]
---

# Phase 7: Giao diện collection trên dashboard

## Overview

Panel quản lý collection và glossary, ô chọn collection trong form tải, bộ lọc theo collection, chuyển
collection cho video, nút "Lưu vào thư mục" kèm trạng thái.

## Requirements

- Panel "Collections" cạnh `ChannelsPanel` (`dashboard/DashboardPage.tsx:136`): danh sách (tên, loại
  kênh / 合集 / tạo tay, số video, thư mục lưu); tạo collection; sửa tên và setting.
  - Ô màu có ô xem trước; để trống nghĩa là dùng giá trị chung.
  - Độ mờ nền là thanh trượt 0–100%.
  - Thư mục lưu nhập dạng thư mục con, hiển thị kèm tiền tố là thư mục gốc trên Mac.
- Tab "Glossary" trong trang sửa collection: bảng 中文, Tiếng Việt, ghi chú, nguồn ("Tự đặt" hoặc
  "Agent" kèm link video nguồn); ô lọc phía client; thêm, sửa tại chỗ, xóa; lỗi validation hiện cạnh ô.
- `JobForm.tsx`: ô chọn "Collection: Tự nhận (mặc định)" hoặc một collection có sẵn.
- `PipelineTable.tsx`: bộ lọc collection (mở rộng `PipelineFilters`, `queryOf` trong
  `DashboardPage.tsx:14`, `VideoListQuery` trong `api/types.ts:139`); cột tên collection và tập.
- `BulkActionBar.tsx` và `VideoDetail.tsx`: "Chuyển collection" và "Lưu vào thư mục".
  - Nút lưu hiện khi có bản cuối. Trên trang video có thể chọn generation đã render.
  - Collection chưa có thư mục thì nút bị tắt kèm gợi ý đặt thư mục.
- `VideoDetail.tsx`: trạng thái lưu gần nhất (đang chờ, đang lưu, đã lưu kèm tên file, lỗi kèm lý do).

## Related Code Files

- Create: `services/web/src/dashboard/CollectionsPanel.tsx`, `dashboard/CollectionGlossary.tsx` và test
- Modify: `services/web/src/api/client.ts`, `api/types.ts`
- Modify: `services/web/src/components/JobForm.tsx`, `dashboard/ChannelsPanel.tsx`,
  `dashboard/PipelineTable.tsx`, `dashboard/DashboardPage.tsx`, `dashboard/BulkActionBar.tsx`,
  `dashboard/VideoDetail.tsx`

## Implementation Steps

1. Test trước (vitest): form gửi `collection_id` khi chọn; bộ lọc đưa `collection` vào query; nút lưu
   tắt khi không có thư mục; trạng thái `failed` hiện lý do; ô màu để trống gửi `null`; sửa một thuật
   ngữ gửi `PUT` đúng khóa; lỗi validation glossary hiện đúng ô.
2. Viết giao diện theo các component hiện có.

## Todo

- [x] Test component
- [x] Panel collection, form setting, tab glossary
- [x] Form tải, bộ lọc, chuyển collection, nút lưu và trạng thái

## Success Criteria

- `pnpm --filter @reup-flow/web test`, `build`, `lint` xanh.
- Mở dashboard thật và thao tác được cả luồng (skill `run` hoặc Chrome).

## Ghi chú triển khai (2026-11-14)

- Component mới là `CollectionsPanel.tsx`, gồm danh sách, tạo collection, form setting và tab Glossary. Glossary nằm ở `CollectionGlossary.tsx`. Hook `useCollections` poll mỗi 10 giây (`useDashboard.ts`). Các helper `fieldErrorsOf`, `COLLECTION_KIND_LABELS`, `SAVE_ROOT_LABEL` và `FINAL_STAGES` nằm trong `format.ts`. Bốn file `useDashboard.ts`, `format.ts`, `test-fixtures.ts` và `ChannelsPanel.tsx` cũng phải sửa, dù danh sách file của phase không ghi hết.
- Khi chọn "Tự nhận (mặc định)", key `collection_id` không được gửi đi. `createJob` và `createVideo` chỉ thêm key này khi có id. Sau mỗi lần gửi, ô chọn quay về mặc định.
- Lỗi 400 của API có dạng `message: string[]`, mỗi phần tử là `"<field>: <message>"`. `ApiError.messages` giữ nguyên danh sách này, và form hiện từng lỗi cạnh đúng ô qua `aria-describedby`.
- Form setting chỉ PATCH những field đã đổi. Để trống nghĩa là `null`, tức dùng giá trị chung. Độ mờ có checkbox "Dùng độ mờ chung".
- Không API nào trả về đường dẫn `REVIEW_WORKER_EXPORT_ROOT`, nên giao diện ghi nhãn trung tính "Thư mục gốc trên Mac/" chứ không bịa đường dẫn.
- Nút lưu ở trang chi tiết luôn gửi `generation`, chọn trong số các generation có `render.state = 'rendered'`. Bản cũ không có render row thì dùng generation hiện tại. Nút lưu hàng loạt không gửi `generation`. Nút chỉ bị tắt khi biết chắc collection không có thư mục. Nếu chưa tải được danh sách collection thì nút vẫn bật, server sẽ kiểm.
- Dòng trạng thái lưu luôn lấy `folder_export` mới nhất từ lần poll. Lý do bị bỏ qua của một lần bấm hiện ở ActionNotice.
- Chưa mở dashboard thật. Container `web` được giữ tắt, và `nest` đang chạy là image cũ chưa có `/api/collections`, nên chưa kiểm được trực tiếp. Phase 8 cần deploy `nest` trước `web`, vì web mới đọc `item.collection`.
- Review: [code-reviewer-261114-1932-collections-phase-07](../reports/code-reviewer-261114-1932-collections-phase-07.md).


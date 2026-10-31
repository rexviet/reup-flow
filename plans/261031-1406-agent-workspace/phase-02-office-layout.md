# Giai đoạn 2: Trang tĩnh, bố cục văn phòng và bàn

Trạng thái: completed · Ưu tiên: P2 · Effort: 5h · Phụ thuộc: giai đoạn 1

## Mục tiêu

Tab "Workspace" trong `services/web` vẽ văn phòng nhìn từ trên từ dữ liệu của snapshot. Chưa có cập nhật sống; một lần tải là một ảnh chụp.

## Bố cục

Bốn khu, mỗi khu một vùng có tiêu đề: **Media** (media, render), **Voice** (stt, tts, voice_check), **LLM** (Localizer, Dubber, Reviewer, Slot Shortener, Ad Classifier, Review JEV), **Cổng** (quảng cáo, cổng 0, needs_human). Mỗi agent hoặc bước là một bàn: tên, trạng thái, video đang xử lý (nếu có), số lần thử khi retry.

Khu Cổng hiển thị video theo kết quả (`skipped_ad`, `skipped_no_video`, `published_original`, `needs_human`), không phải theo bàn.

## Việc cần làm

1. Thêm điều hướng tab trong `services/web/src/App.tsx` (hiện chỉ có trang jobs); không thêm thư viện router nếu hai tab là đủ, theo đúng phong cách tối giản hiện có.
2. `services/web/src/api/client.ts`: thêm `getWorkspaceSnapshot()` bằng cùng hàm `request` và `ApiError`.
3. `services/web/src/workspace/`:
   - `OfficeMap.tsx`: SVG hoặc lưới CSS, quyết định khi bắt đầu giai đoạn bằng một mockup nhỏ; mặc định lưới CSS vì Tailwind đã có và dễ test.
   - `Desk.tsx`: một bàn, nhận trạng thái và hiển thị bằng màu cộng nhãn chữ (không chỉ dựa vào màu).
   - `TopBar.tsx`: nguồn dữ liệu (Temporal, Postgres), thời điểm cập nhật; chữ "Dữ liệu thật" chỉ hiện khi cả hai nguồn `ok`, còn nếu không hiện nguồn nào đang lỗi.
   - `workspace.layout.ts`: danh sách khu và bàn cố định, khớp `agentId` của catalog ở giai đoạn 1 (kiểu dùng chung, không sao chép chuỗi).
4. Trạng thái rỗng: bàn không có dữ liệu hiện "rảnh", toàn bộ nguồn lỗi hiện thông báo rõ ràng và không vẽ bàn "đang làm" nào.
5. Hỗ trợ `prefers-reduced-motion` ngay từ đầu (chưa có animation nhưng quy ước giữ cho giai đoạn 3).

## File sở hữu

Tạo mới `services/web/src/workspace/*`; sửa `services/web/src/App.tsx`, `services/web/src/api/client.ts`, `services/web/src/api/types.ts`, `services/web/src/index.css` nếu cần.

## Kiểm thử

- Component test (Testing Library, theo mẫu `JobCard.test.tsx`): mỗi trạng thái bàn render đúng nhãn; trạng thái rỗng; nguồn `down`.
- Test rằng bàn không bao giờ hiển thị "đang làm" khi snapshot không có agent ở trạng thái đó.
- Kiểm tay trên trình duyệt với dữ liệu thật; chụp ảnh để người dùng duyệt bố cục.

## Nghiệm thu

- Đủ bốn khu và mọi bàn trong catalog, kể cả khi không có dữ liệu.
- Màn hình điện thoại không cuộn ngang.
- `pnpm --filter @reup-flow/web lint`, `test`, `build` xanh.

## Rollback

Bỏ thư mục `workspace/` và hoàn các sửa nhỏ trong `App.tsx`, `client.ts`, `types.ts`.

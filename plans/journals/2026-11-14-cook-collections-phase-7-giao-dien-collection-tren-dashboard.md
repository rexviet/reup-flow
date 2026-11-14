---
title: "Cook collections phase 7: giao diện collection trên dashboard"
date: 2026-11-14
summary: "Dashboard có panel Collections (setting, glossary), ô chọn collection khi tải, bộ lọc và cột collection, chuyển collection, nút Lưu vào thư mục kèm trạng thái. Mọi request được đối chiếu với controller thật, và mọi nhánh chính đã kiểm bằng mutation."
---

# Cook collections phase 7: giao diện collection trên dashboard

## Việc đã làm

- `services/web` thêm `CollectionsPanel` gồm danh sách, tạo collection và form setting có ô xem trước màu, thanh trượt độ mờ và thư mục con kèm tiền tố. Tab Glossary nằm ở `CollectionGlossary`, có lọc phía client, thêm, sửa tại chỗ và xóa.
- `JobForm` có ô chọn collection.
- `PipelineTable` có bộ lọc `collection` và cột tên collection cùng số tập.
- `BulkActionBar` và `VideoDetail` có "Chuyển collection" và "Lưu vào thư mục". Trang chi tiết hiện trạng thái lưu gần nhất.

## Những chỗ đáng nhớ

- Đối chiếu type với server thì thấy lỗi 400 của `ZodValidationPipe` có `message` là mảng. Client cũ chỉ đọc chuỗi, nên lỗi validation chỉ hiện ra "Bad Request". `ApiError` giờ giữ cả danh sách để form gắn từng lỗi vào đúng ô.
- `vi.mock('../api/client')` tự mock cả helper trong module đó. Vì vậy hàm tách lỗi theo field được đặt ở `format.ts`, chứ không ở `client.ts`. Nếu đặt trong `client.ts`, test component sẽ chạy bản mock trả `undefined`.
- Thêm tham số thứ ba `collectionId` thì test cũ `toHaveBeenCalledWith(url, true)` đỏ, vì Vitest so cả độ dài mảng đối số. Tôi sửa assertion để ghi rõ `undefined` là đối số thứ ba. Như vậy assertion chặt hơn, không lỏng đi.
- Không API nào trả về đường dẫn gốc trên Mac, nên tôi ghi nhãn "Thư mục gốc trên Mac/" thay vì đoán một đường dẫn.
- Container `nest` đang chạy là image cũ: `PUT /api/collections/...` trả "Cannot PUT". Vì vậy chưa kiểm trực tiếp được, chỉ đối chiếu với mã nguồn. Phase 8 phải deploy `nest` trước `web`.
- Review bắt hai lỗi thật.
  - Lý do bị bỏ qua của một lần bấm lưu che mất trạng thái lưu thật, và poll không bao giờ xóa nó.
  - Lỗi `term_zh` khi sửa tại chỗ không hiện ở đâu cả.
  
  Review cũng chỉ ra hai nhánh "chưa tải được collection thì nút vẫn bật" chưa có test nào đỏ được. Mỗi chỗ đều có test đỏ trước, sửa, rồi kiểm lại bằng mutation.
- Có 19 mutation, mỗi cái gỡ một nhánh, gồm gửi `collection_id: null`, bỏ `collection` khỏi query, bỏ lý do lỗi hoặc danh sách file khỏi trạng thái, bỏ điều kiện tắt nút khi không có thư mục, PUT sai khóa, bỏ NFC khi kiểm trùng thuật ngữ và PATCH mọi field. Mutation nào cũng làm test đỏ.

## Còn mở

- Chưa mở dashboard thật. Việc này cần stack mới, để dành cho phase 8.
- Câu hỏi sản phẩm 1: ô chọn collection có nên giữ giữa các lần tải không? Hiện tại nó quay về mặc định sau mỗi lần gửi.
- Câu hỏi sản phẩm 2: sửa thuật ngữ của agent có nên cảnh báo rằng nó sẽ thành "Tự đặt" không?
- Thuật ngữ đúng bằng `.` hoặc `..` không gửi được qua URL. Muốn sửa phải đổi cách mã hóa khóa ở server.

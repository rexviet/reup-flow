---
title: "Cook collections phase 3: gán collection lúc tải"
date: 2026-11-14
summary: Mọi đường ghi video gán collection theo thứ tự chọn tay → 合集 → kênh; video cũ được chuyển vào 合集; review bắt hai chỗ lệch spec về lựa chọn tay.
---

# Cook collections phase 3: gán collection lúc tải

## What happened

Phase 3 thay bản tạm của phase 2 trong `upsertMany` (luôn nhét video mới vào collection kênh) bằng `resolveCollections` và `intakeVideos` trong `services/downloader/src/db/collections.repository.ts`. Enumerate, video lẻ và job kênh cùng đi qua một đường: resolve collection → `upsertMany` (giờ nhận collection tường minh, không tự tạo gì) → `moveIntoMixes` (chuyển video đang ở collection kênh mặc định, chưa chọn tay, vào 合集; điền số tập còn thiếu). `POST /api/jobs` và `POST /api/videos` nhận `collection_id`; id sai hoặc không tồn tại trả 400 trước khi gọi parser.

Test chạy trên trang enumerate thật của IM-薛珍珠: 10 tập 成名的代价 vào 合集 với tập 1–10, video còn lại vào collection kênh tên IM-薛珍珠, chạy lại không sinh collection mới, video giả lập sau backfill được chuyển sang 合集 trừ video đánh dấu tay.

## Những chỗ đáng nhớ

- Test đồng thời không dựa vào `Promise.all`: writer A tạo 合集 trong transaction để mở, writer B chạy trên kết nối riêng, test đợi tới khi `pg_stat_activity` báo B ở `wait_event_type = 'Lock'` rồi mới commit A. Phải dựng sẵn tên kênh và nickname để chỗ chờ duy nhất của B là câu insert 合集; nếu không, B chờ ở câu insert collection kênh và test chứng minh nhầm thứ.
- Kiểm đột biến bắt được hai điều mà suite xanh không cho thấy. (1) Bốn đột biến "bị giết" chỉ vì lỗi biên dịch; chạy lại ở dạng hợp kiểu thì cả bốn mới thật sự bị test hành vi giết. (2) Sau khi sửa M1, việc áp lựa chọn tay hai lần (trong `intakeVideos` và trong `placeByHand`) làm một nhánh thừa: đột biến bỏ nhánh đó sống sót. Giới hạn `placeByHand` cho video đã có (`inserted === 0`) thì mỗi nhánh mới có tác dụng riêng. Đột biến còn sống duy nhất là đột biến tương đương.
- Review tìm ra hai chỗ tôi tự đặt luật hẹp hơn spec. M2: job có collection chọn tay đã chặn luôn việc chuyển video cũ vào 合集, trái với `plan.md` (chỉ miễn video đã chọn tay); tiêu chí "tải lại kênh thì 10 tập vào 合集" sẽ trượt nếu người dùng chọn tay. M1: video lẻ đã có mà form gửi `collection_id` thì bị bỏ qua lặng lẽ và vẫn trả 202.

## Decision

- Collection chọn tay của job chỉ áp cho video mới; video cũ chưa chọn tay vẫn vào 合集.
- Ở đường video lẻ, `collection_id` của form áp cả cho video đã có (chuyển và đánh dấu tay), vì spec không giới hạn "video mới" cho đường này. Cần người dùng xác nhận.
- Vẫn luôn tạo collection kênh, vì glossary của kênh dựa vào nó cho tới khi phase 5 xong.

## Next steps

- Phase 4: map lỗi FK `23503` khi có đường xóa collection; chốt tiền tố `E01-` dựa vào `mix_episode` hay loại collection (video xếp tay vẫn giữ số tập).
- Phase 7: bỏ hẳn khóa `collection_id` khi không chọn (null bị 400).
- Phase 8: 合集 thuộc kênh nhìn thấy nó đầu tiên.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

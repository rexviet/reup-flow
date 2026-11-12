---
title: Plan admin dashboard và red team
date: 2026-11-11
summary: "Lập plan dashboard gộp Jobs/Review/Workspace; red team tìm 15 lỗi, áp 14"
---

# Plan admin dashboard và red team

## What happened
Lập plan `plans/261111-1644-admin-dashboard` (7 phase, ~53h) gộp tab Jobs, Review và Workspace thành một dashboard theo dõi từng video, kèm duyệt rồi tự render bản cuối bằng worker pm2 trên máy. Red team 4 reviewer tìm 15 phát hiện (4 Critical); người dùng duyệt từng cái: áp 14, bỏ 1 (test ánh xạ activity → bước con).

## Phát hiện đáng nhớ
- Run canary tạo generation mới mà không cập nhật outbox: DB thật có outbox g1 nhưng runs tới g3/g7. Tăng generation từ outbox sẽ trùng workflow ID cũ và starter nuốt `WorkflowAlreadyStartedError`.
- Studio Export ghi `renders/*.meta.json`; `Repository.projection` tính cả file này nên project bị coi là chưa lưu mãi.
- `<video>` không gửi được header `X-Api-Key`, nên media cần URL ký HMAC.
- Cổng 3300/5173/5433/6379 đang mở trên mọi interface; web gọi API bằng URL tuyệt đối nên tunnel sẽ hỏng.
- `listOpen` của workspace giới hạn 10 workflow, bỏ sót Vbee.

## Decision
Temporal giữ chỉ đọc; video từ job kênh mặc định bị giữ, extension vẫn localize ngay; duyệt gắn revision và worker render từ revision bất biến ra thư mục tạm (bỏ được cổng "Studio đang mở"); nút Mở Studio là yêu cầu qua DB cho worker trên máy.

## Next steps
Trả lời câu hỏi mở về test ánh xạ activity, rồi `/ak-cook plans/261111-1644-admin-dashboard/plan.md`.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

---
phase: 8
title: Web UI
status: not_started
effort: ~1d
blockedBy: [3]
milestone: M2
---

# Phase 8 — Web UI

Chỉ cần Phase 3 (API) ⇒ **làm song song được** với 4–7.

Đọc trước: brainstorm §10 · IDEA.md §7.

---

## 8.1 Một trang, React + Vite + Tailwind

- Ô input URL profile + nút "Download all"
- List job đang chạy / đã xong
- **Hai thanh tiến độ RIÊNG BIỆT** mỗi job:
  - Thanh 1 enumerate: "Đang quét: 340 video" — **indeterminate** cho tới `hasMore=0`
  - Thanh 2 download: "127 / 340"
- Badge cookie từ `/internal/cookie/status`: xanh <30′ · vàng cũ hơn · **đỏ nếu không có**
- Nút cancel / retry failed
- Polling 1s

## 8.2 Vì sao KHÔNG gộp thành một %

Trước khi enumerate xong thì **mẫu số chưa tồn tại** (`total_videos` là NULL). Ghép vào sẽ ra progress bar nhảy lung tung — con số tụt xuống khi enumerate tìm thêm video. Hai thanh là bắt buộc, không phải thẩm mỹ.

## 8.3 Badge cookie là thứ đầu tiên cần nhìn khi job fail

Đặt ở vị trí luôn thấy được, không phải trong menu. Phần lớn job `blocked` là do cookie, và badge trả lời câu đó trong một cái nhìn.

## 8.4 F7 — hiển thị cho đúng

Job chạy lần 2 trên profile không có video mới sẽ hiện **`340/340` ngay lập tức**. Đây là **thông tin đúng** ("mọi thứ đã tải xong"), không phải bug.

Thêm nhãn `"0 video mới"` khi `enumerated_count == 0` để không ai hiểu nhầm là hỏng.

## 8.5 Không có ô paste cookie

Lệch có chủ ý so với IDEA.md §9 M1. Cookie push bằng `curl` → `POST /internal/cookie` cho tới khi extension (phase 9) sẵn sàng. **Không xây ô text sẽ bị xoá ở M3.** Cùng một bằng chứng, không tốn công.

---

## Todo

- [ ] Vite + React + Tailwind trong `services/downloader/web` hoặc `services/web`
- [ ] Form tạo job
- [ ] List job + polling 1s
- [ ] Hai thanh riêng, thanh 1 indeterminate
- [ ] Badge cookie 3 trạng thái
- [ ] Cancel / retry
- [ ] Nhãn "0 video mới" (F7)
- [ ] Compose: thêm service `web`

## DoD

- [ ] Dán URL → job xuất hiện, tiến độ chạy
- [ ] Thanh enumerate indeterminate cho tới khi `total_videos` có giá trị
- [ ] Xoá key cookie trong Redis → badge **đỏ** trong ≤2s
- [ ] Cookie push 40′ trước → badge **vàng**
- [ ] Chạy lại profile đã xong → hiện `340/340` + nhãn "0 video mới"
- [ ] Polling tiếp tục sau khi mất mạng tạm rồi có lại (không cần code reconnect)

## Risks

| Risk | Xử lý |
|---|---|
| Ai đó gộp 2 thanh thành 1 % | Comment ghi rõ mẫu số NULL. §8.2 |
| Polling 1s × nhiều tab → tải API | Internal tool 1–3 người. Nếu thành vấn đề thì mới cân SSE, không xây trước |
| `340/340` bị báo là bug | Nhãn "0 video mới" + ghi trong README |

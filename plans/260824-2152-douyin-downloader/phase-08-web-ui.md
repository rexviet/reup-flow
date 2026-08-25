---
phase: 8
title: Web UI
status: done_with_concerns
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

- [x] Vite + React + Tailwind trong `services/web`
- [x] Form tạo job
- [x] List job + polling 1s
- [x] Hai thanh riêng, thanh 1 indeterminate
- [x] Badge cookie 3 trạng thái
- [x] Cancel / retry
- [x] Nhãn "0 video mới" (F7)
- [ ] Compose: thêm service `web` — **hoãn có chủ ý**. `docker-compose.yml` là file
      dùng chung giữa 3 agent chạy song song trong cùng working tree; agent phase 8
      không sửa file này. Block YAML cần thêm + lý do (Vite inline env lúc build,
      không phải runtime) nằm ở `services/web/README.md` §"Deploy" cho agent điều
      phối áp dụng.

## DoD

- [x] Dán URL → job xuất hiện, tiến độ chạy — verify bằng test tích hợp
      `src/App.test.tsx` (mock API, fixture đúng shape thật), **không** verify
      live: container `nest` đang chạy trong compose stack build từ trước khi
      phase này bắt đầu (`/api/jobs` 404), và không được rebuild/restart theo
      ràng buộc chia sẻ stack với 2 agent khác. Xem README §"Manual verification".
- [x] Thanh enumerate indeterminate cho tới khi `total_videos` có giá trị —
      test + mutation đỏ đã chứng minh (`ProgressBars.test.tsx`)
- [ ] Xoá key cookie trong Redis → badge **đỏ** trong ≤2s — logic đã unit-test
      (`cookieBadgeState`, mutation đỏ), polling 1s đã test, nhưng **chưa verify
      live** vì lý do container nêu trên
- [ ] Cookie push 40′ trước → badge **vàng** — cùng lý do, chưa verify live
- [x] Chạy lại profile đã xong → hiện `340/340` + nhãn "0 video mới" — verify
      bằng fixture mô phỏng đúng F7 (`total_videos`/`downloaded_count` đầy đủ từ
      job trước, `enumerated_count` job hiện tại = 0), mutation đỏ đã chứng minh;
      chưa chạy được với dữ liệu Douyin thật
- [x] Polling tiếp tục sau khi mất mạng tạm rồi có lại (không cần code
      reconnect) — `usePolling.test.ts`, mutation đỏ đã chứng minh

## Risks

| Risk | Xử lý |
|---|---|
| Ai đó gộp 2 thanh thành 1 % | Comment ghi rõ mẫu số NULL. §8.2 |
| Polling 1s × nhiều tab → tải API | Internal tool 1–3 người. Nếu thành vấn đề thì mới cân SSE, không xây trước |
| `340/340` bị báo là bug | Nhãn "0 video mới" + ghi trong README |

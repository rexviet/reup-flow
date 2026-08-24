---
phase: 0
title: Spikes — xác nhận thiết kế trước khi xây
status: not_started
effort: 1-2 ngày
blockedBy: []
gate: "GATE A — có thể huỷ dự án hoặc lật D3"
---

# Phase 0 — Spikes

**Không viết một dòng code sản phẩm nào ở phase này.** Mọi thứ là script tạm, notebook, hoặc curl. Output là **số và câu trả lời**, ghi vào `docs/reports/spike-results-2026-08-24.md`.

Xếp theo mức độ có thể lật thiết kế.

---

## S1 — Nghe thử voice clone tiếng Việt · **chặn cả dự án**

**Vì sao:** chỉ có một ngôn ngữ đích. Nếu giọng talent Trung Quốc đọc tiếng Việt nghe tệ thì không có đường lùi nào trong thiết kế.

**Làm:**
1. Dựng OmniVoice Studio (self-host hoặc Colab). Ghi lại base URL tunnel.
2. `GET /v1/audio/voices` — lấy danh sách voice, xác nhận format `voice_id`.
3. Clone giọng 1 talent thật từ ~3–10s audio sạch.
4. Tổng hợp 3 đoạn tiếng Việt: một câu ngắn, một câu có số ("giảm 50%, chỉ 199 nghìn"), một câu có tên riêng.
5. **Gộp spike #2 cũ vào đây:** lấy 1 clip Douyin thật, gọi
   ```
   POST /batch/enqueue   (multipart)
     video: <clip.mp4>
     langs: "vi"
     voice_id: <voice vừa clone>
     preserve_bg: true
   ```
   rồi `GET /batch/jobs/{job_id}` tới `done`, `GET /batch/download/{job_id}/vi`.
6. **Nghe bằng tai người.** Không có metric nào thay được bước này.

**Ghi lại:**
- Nhạc nền Douyin còn không? (`preserve_bg=true` có hoạt động thật không)
- Giọng có drift giữa các câu không? (mặc định `voice_match="per_line"`)
- `timing_strategy="concise"` cắt mất nghĩa bao nhiêu?
- Tên riêng tiếng Trung bị dịch máy thành gì?
- Thời gian job: bao nhiêu giây cho clip bao nhiêu giây?

**Exit criteria:** một câu trả lời rõ ràng **nghe được / không nghe được**, kèm 3 file audio để người khác nghe lại được.

**Nếu fail:** thử engine khác trong 14 engine ở Settings **trước khi** kết luận. Đây là mitigation duy nhất của risk này.

---

## S2 — Facebook: cả chuỗi onboarding + publish + Reels · **chặn D3 và D6**

**Vì sao:** D3 đổi endpoint là quyết định lớn nhất của Rev 3, và rủi ro duy nhất còn lại của nó ("có vào Reels không") chỉ trả lời được bằng thực nghiệm.

**Làm — đúng thứ tự này:**

1. Tạo FB app, để **Development mode**. Thêm người vận hành làm admin/developer.
2. Graph API Explorer → tick `pages_show_list` + `pages_read_engagement` + `pages_manage_posts` → Generate Access Token. Copy **user token ngắn hạn**.
3. ```
   GET /oauth/access_token?grant_type=fb_exchange_token
       &client_id=<APP_ID>&client_secret=<APP_SECRET>&fb_exchange_token=<token ngắn hạn>
   ```
   → xác nhận trả về user token dài hạn, ghi lại `expires_in`.
4. `GET /me/accounts?fields=id,name,access_token` → **xác nhận trả page token cho MỌI page người đó quản lý**, không phải chỉ một vài.
5. **Câu quan trọng nhất của spike này:** dán page token thu được vào `GET /debug_token`.
   → **`expires_at` có thật sự là `0`?** Hay là vắng mặt? Hay là một số?
   Docs không nói rõ hình dạng response, mà phép kiểm nhị phân của D6 dựa vào đúng con số này.
6. Đăng 1 video test:
   ```
   POST /{page-id}/videos    (multipart)
     source: <video.mp4>
     description: "test"
     published: false
     scheduled_publish_time: <now + 20 phút>
   ```
   → ghi lại `video_id`.
7. `GET /{video-id}` — đọc được trạng thái không? Field nào cho biết đã publish?
8. Chờ tới giờ hẹn. **Mở page, kiểm bài đó có xuất hiện ở tab Reels không.** Đây là câu hỏi lật D3.
9. `DELETE /{video-id}` — xoá được không?

**Ghi lại:** hình dạng chính xác của `expires_at` · bài có vào Reels hay không · video test dùng aspect ratio gì và duration bao nhiêu (để biết kết luận áp cho loại video nào) · error code khi `scheduled_publish_time` sai.

**Exit criteria:** cả 9 bước có kết quả ghi lại.

**Nếu bước 8 fail (không vào Reels):** chuyển D3 sang **Approach 3** (dùng cả hai endpoint) và cập nhật solution design. Phase 6 sẽ có hai code path publish, hai horizon (29 ngày vs 6 tháng), và **top-up lane quay lại** — effort Phase 6 gấp đôi. Đây là lý do spike này phải xong trước.

---

## S3 — Douyin: dữ liệu bắt buộc + fork có public không · **chặn Module 1**

**Làm:**
1. ~~Kiểm fork có public không~~ → **XONG 2026-08-24.** Fork **public** (`api.github.com/repos/rexviet/...` trả 200 không cần auth), và cookie ở `crawlers/douyin/web/config.yaml` là **của upstream** — commit `3b491db` bởi `Evil0ctal`, 2025-03-16, tức đã public trong repo gốc từ 2024. **Không phải cookie của ta, không cần thu hồi.** Việc còn lại chỉ là scrub ở Phase 1.
2. Gọi endpoint enumerate của fork cho 1 talent thật. Dump response JSON.
3. Xác nhận `aweme_list[]` có **cả**:
   - `desc` — caption gốc, dữ liệu **bắt buộc** cho caption pipeline
   - `statistics{play_count, digg_count, comment_count, share_count}` — nguyên liệu curation
   - `video.play_addr` — để không phải re-resolve từng video
   - `video.{width,height,duration}` + fps
4. Đo **TTL thật của `play_addr`**: lấy URL, thử tải lại sau 1h / 2h / 4h, ghi lúc nào 403.
5. Đo rate limit thật: gọi enumerate tuần tự, xem tới request thứ mấy thì bị chặn / trả `aweme_list` rỗng.

**Exit criteria:** một file JSON mẫu đã lưu + con số TTL + con số rate limit.

**Nếu `desc` không có trong `aweme_list[]`:** caption pipeline phải thêm 1 lời gọi API/video — tính lại budget rate limit của Phase 3.

---

## S4 — Benchmark GPU

**Làm:** chạy 1 video 30 giây qua hết chuỗi `/batch/enqueue` **trên đúng cấu hình sẽ dùng thật** (không phải máy dev).

**Ghi lại:** giây GPU / giây video · RAM peak · dung lượng file trung gian (demucs sinh ra `vocals.wav` + `no_vocals.wav`).

**Exit criteria:** con số thay cho ước lượng "8–17 giờ cho 1.000 video" ở §10 của solution design.

---

## S5 — Xác nhận Colab đủ hay không

**Làm:** để 1 session Colab chạy OmniVoice, đo:
- Session sống được bao lâu trước khi chết / bị preempt
- URL tunnel đổi mỗi lần restart đúng như dự đoán không
- Restart giữa lúc có job đang chạy → job mất hoàn toàn đúng không (`_jobs = {}` in-memory)

**Exit criteria:** xác nhận §6.5 (base URL đọc Redis mỗi request + `POST /internal/omnivoice`) là cần thiết, không phải phòng xa.

---

## Output của Phase 0

`docs/reports/spike-results-2026-08-24.md` với đúng 5 mục S1–S5, mỗi mục có **số hoặc câu trả lời**, không có "có vẻ như".

Rồi **cập nhật solution design lên Rev 6** nếu có spike nào lật một quyết định.

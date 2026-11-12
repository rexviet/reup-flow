# Admin dashboard

Một trang trong `services/web` cho một admin trên máy này: mỗi video là một dòng, đi qua các
giai đoạn từ tải tới phát hành. Mở `http://localhost:5173`; chi tiết một video ở
`#/video/<aweme_id>`. Dữ liệu ghép từ bảng của downloader, `localization.*`, Temporal (chỉ đọc)
và heartbeat của worker review trên máy ([review-companion](review-companion.md#render-bản-cuối-trên-máy-reup-review-worker)).
API: `GET /api/dashboard/{videos,videos/:awemeId,attention,channels,health}`,
`POST /api/dashboard/actions`, `POST /api/dashboard/media-links`, `GET /api/dashboard/media/:token`.

Giao diện là theme tối (token màu, font Inter và JetBrains Mono nằm trong `services/web/tailwind.config.js`;
font được đóng gói bằng npm, icon là SVG nội tuyến nên không tải gì từ CDN). Thanh bên chỉ có các route
thật: bảng điều khiển và video đang mở.

## Giai đoạn

Mỗi video luôn được tính trên **generation hiện tại**: số lớn nhất trong generation của outbox,
của `localization.runs` và của workflow đang mở trong Temporal. Canary và chạy lại tạo generation
mới mà outbox không biết (ví dụ video đang ở g7 rồi canary tạo g8), nên chỉ đọc outbox sẽ báo sai.
Dòng `runs` (workflow đã kết thúc) thắng workflow mở nếu hai nguồn đọc lệch nhau.

| Giai đoạn | Ý nghĩa |
|---|---|
| Đang tải / Tải lỗi | job tải của downloader chưa xong hoặc thất bại |
| Đang giữ (`held`) | tải xong nhưng chưa được đưa vào localization (kênh tắt "Tự localize"); cũng là video chưa từng vào hàng đợi |
| Chờ chạy (`queued`) | outbox `pending`, hoặc đã gửi mà chưa thấy workflow |
| Đang localize | workflow mở trong Temporal; kèm bước con: chuẩn bị, dịch, lồng tiếng, kiểm tra. Activity lạ hiện "không rõ" |
| Gửi lỗi / Chạy lỗi | outbox `rejected` hoặc `dead`; run `failed` hoặc kết thúc không hợp lệ |
| Bỏ qua | quảng cáo hoặc không có video |
| Chờ duyệt | run `ready_for_review` hoặc `needs_human`, chưa có quyết định |
| Đã trả về | admin trả lại để sửa tiếp |
| Chờ render / Đang render / Render lỗi | đã duyệt một revision; worker review trên máy dựng và render đúng revision đó |
| Đã render | có MP4 và SRT của revision đã duyệt trong MinIO |
| Đã đăng | admin đánh dấu đã đăng (kèm platform và link) |

Dấu `?` cạnh giai đoạn nghĩa là một nguồn cần để quyết định (Temporal hoặc Postgres) không đọc
được, giai đoạn chỉ là phỏng đoán tốt nhất. Video đã qua bước tải khi đó không bị hiện là "rảnh".

## Cần xử lý

Hộp "Cần xử lý" liệt kê mỗi lý do kèm việc nên làm:

| Mã | Khi nào |
|---|---|
| `download_failed` | tải thất bại |
| `dispatch_failed` | đầu vào bị từ chối (`rejected`): chạy lại không giúp, kiểm tra file gốc |
| `dispatch_dead` | gửi sang localization thất bại nhiều lần |
| `run_failed` | lần chạy localization thất bại |
| `render_failed` | render bản cuối thất bại |
| `needs_human` | pipeline đánh dấu cần người xem |
| `mismatch` | còn cue lời đọc lệch phụ đề (khi chờ duyệt hoặc đã trả về) |
| `dirty` | project Studio có thay đổi chưa lưu |
| `retrying` | một activity đang thử lại lần thứ 2 trở lên |
| `stuck` | một activity chạy quá `DASHBOARD_STUCK_MINUTES` (mặc định 30) |
| `lost` | đã gửi hơn 10 phút, Temporal không có workflow và chưa có run |
| `old_generation_awaiting_review` | một generation cũ có project vẫn chờ duyệt, auto-open có thể mở nhầm |

## Thao tác

Mọi thao tác đi qua `POST /api/dashboard/actions` (tối đa 100 video, trả `done` và `skipped` kèm lý
do từng video). Mỗi video một transaction, điều kiện được kiểm lại ở server.

| Thao tác | Điều kiện |
|---|---|
| Đưa vào localization | outbox đang `held` (chỉ đường này chuyển `held` sang `pending`) |
| Chạy lại | outbox `dispatched` hoặc `dead`, không còn workflow mở nào (kể cả canary), Temporal đọc được, không đang render. Tạo generation mới (hiện tại + 1) cho cả run. Không chạy lại `rejected` |
| Duyệt | một video mỗi lần, đúng revision admin đang xem, chưa có thay đổi chưa lưu, đã xác nhận nếu còn mismatch, không đang render |
| Trả về | đang chờ duyệt, chưa đăng, không đang render; xóa dòng render của generation này |
| Render lại | render đang `failed` hoặc `rendered` |
| Đánh dấu / bỏ đánh dấu đã đăng | video đã render; đánh dấu chỉ có hiệu lực một lần, giữ platform và link cũ |
| Mở Studio | ghi yêu cầu, worker trên máy mở Studio |

Duyệt gắn với revision: nếu một revision mới được lưu trước khi worker render xong, render báo bản
đã đổi sau khi duyệt thay vì dựng bản chưa duyệt.

## Badge sức khỏe

Thanh trên cùng, poll 10 giây: Postgres, Redis, cookie Douyin, Temporal, MinIO, VoiceStudio, bảy
hàng đợi Temporal (`stt`, `tts`, `media`, `render`, `llm`, `io`, `workflows`, đo bằng poller còn
tươi), worker review trên máy (heartbeat; quá 2 phút là `down`) và đĩa (dưới 20 GB `degraded`, dưới
5 GB `down`). VoiceStudio và đĩa lấy từ heartbeat của worker review, không phải từ container.
Tắt `reup-voice-worker` thì `stt` và `tts` đỏ; tắt VoiceStudio thì badge VoiceStudio đỏ (khi worker
review còn chạy). Tắt worker review thì badge worker đỏ, còn VoiceStudio và đĩa chuyển `unknown` vì
số liệu cũ không được hiện như sự thật.

## Giới hạn

- Không hủy một run localization (dùng Temporal UI); downloader giữ Temporal chỉ đọc.
- Không chạy lại một bước riêng lẻ: chạy lại là một generation mới cho cả run.
- Nút **Mở Studio** chỉ mở Studio trên máy chạy dashboard và thay Studio đang mở (có sao lưu).
- Một người dùng; không có đăng nhập riêng. Phát hành ở MVP là đánh dấu tay.
- Media (video gốc, bản cuối, SRT) phát và tải qua link ký HMAC ngắn hạn, cần `DASHBOARD_MEDIA_SECRET`
  (tối thiểu 32 ký tự); thiếu khóa thì media trả 503. Các bản render theo revision được giữ
  trong MinIO, không dọn tự động.

## Truy cập từ xa

Mọi cổng host bind `127.0.0.1`. Để xem từ ngoài, dùng cloudflared tunnel:

- Tunnel **chỉ** trỏ tới `http://localhost:5173` (nginx của web, proxy `/api` và `/internal`).
- Không bao giờ tunnel: 3300 (API), 3002 (Studio), 3310 (review-bridge), 5433 (Postgres), 6379
  (Redis), 9000/9001 (MinIO), 7233/8233 (Temporal), 3100 (Langfuse).
- Bật Cloudflare Access cho hostname **trước** khi mở tunnel: `VITE_API_KEY` nằm trong bundle của
  web, nên bất kỳ ai tới được trang đều dùng được API. Điều này chỉ an toàn khi các cổng khác đã
  bind loopback.
- Qua tunnel, nút Mở Studio vẫn mở Studio trên máy host, không phải trên máy đang xem.

## Vận hành

`scripts/start-all.sh` khởi động Docker và ba tiến trình pm2 (`reup-voice-worker`,
`reup-review-auto-open`, `reup-review-worker`). Đổi code `nest` hoặc `web` thì build lại image
(`docker compose up -d --build --no-deps nest web`). Chi tiết worker review, Chrome và render:
[review-companion](review-companion.md). Màn hình Workspace 2D đã gỡ; xem
[agent-workspace](agent-workspace.md).

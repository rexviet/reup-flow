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

Khi lọc giai đoạn **Đã render**, danh sách hiện dạng lưới ảnh thay vì bảng: mỗi video là một thẻ với
ảnh của bản render cuối (kèm mô tả, id, generation, thời lượng), bấm vào thẻ để mở chi tiết, ô chọn
ở góc thẻ dùng cho thanh thao tác hàng loạt. Ảnh là một khung hình lấy ở giây thứ 1 của MP4 cuối, rộng
480 px, do worker review trên máy tạo ngay sau khi render và lưu cạnh video (`<revision>.mp4` thành
`<revision>.jpg`, không có cột nào trong database); dashboard lấy ảnh qua link ký loại `poster`. Video
chưa có ảnh hiện ô "Chưa có ảnh"; "Render lại" tạo ảnh mới. Tạo ảnh lỗi không làm hỏng render. Mỗi lần
khởi động, worker cũng thử tạo bù ảnh cho các video đã render trước đó, nhưng việc này cần đọc `output/`
và policy MinIO của tài khoản `localization` chỉ được ghi ở đó, nên mặc định nó dừng với một cảnh báo; chỉ
có tác dụng khi bạn chủ động cấp `s3:GetObject` trên `output/*` cho tài khoản này.

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
| Tải lại | video đang ở "Tải lỗi" (`videos.status = failed`); đặt lại về chờ tải với số lần thử mới, lane tải tự nhặt. Thanh thao tác hàng loạt chỉ hiện các nút hợp với giai đoạn của video đã chọn |
| Đưa vào localization | outbox đang `held` (chỉ đường này chuyển `held` sang `pending`) |
| Chạy tiếp từ bước lỗi | outbox `dispatched`, run của generation hiện tại ghi `failed`, không còn workflow mở, không đang render. Giữ nguyên generation và các bước đã xong: dashboard chỉ đặt dòng outbox về `pending`, starter thấy workflow `video-<id>-g<n>` đã `FAILED` thì reset nó về bước lỗi (Temporal chạy lại từ đó, các bước trước lấy kết quả từ lịch sử). Chỉ dùng được khi run lỗi ở một activity; lỗi trong code workflow phải "Chạy lại" |
| Chạy lại | outbox `dispatched` hoặc `dead`, không còn workflow mở nào (kể cả canary), Temporal đọc được, không đang render. Tạo generation mới (hiện tại + 1) cho cả run. Không chạy lại `rejected`. Có ở "Đã trả về": bỏ bản đang sửa dở và dịch lại từ đầu; muốn giữ bản đó thì dùng "Mở Studio" |
| Duyệt | một video mỗi lần, đúng revision admin đang xem, chưa có thay đổi chưa lưu, đã xác nhận nếu còn mismatch, không đang render |
| Trả về | đang chờ duyệt, chưa đăng, không đang render; xóa dòng render của generation này |
| Render lại | render đang `failed` hoặc `rendered` |
| Đánh dấu / bỏ đánh dấu đã đăng | video đã render; đánh dấu chỉ có hiệu lực một lần, giữ platform và link cũ |
| Mở Studio | ghi yêu cầu, worker trên máy mở Studio |

Duyệt gắn với revision: nếu một revision mới được lưu trước khi worker render xong, render báo bản
đã đổi sau khi duyệt thay vì dựng bản chưa duyệt.

## Badge sức khỏe

Thanh trên cùng, poll 10 giây: Postgres, Redis, cookie Douyin, Temporal, MinIO, VoiceStudio, tám
hàng đợi Temporal (`stt`, `tts`, `ingest`, `media`, `render`, `llm`, `io`, `workflows`, đo bằng poller còn
tươi), worker review trên máy (heartbeat; quá 2 phút là `down`) và đĩa (dưới 20 GB `degraded`, dưới
5 GB `down`). VoiceStudio và đĩa lấy từ heartbeat của worker review, không phải từ container.
Tắt `reup-voice-worker` thì `stt` và `tts` đỏ; tắt VoiceStudio thì badge VoiceStudio đỏ (khi worker
review còn chạy). Tắt worker review thì badge worker đỏ, còn VoiceStudio và đĩa chuyển `unknown` vì
số liệu cũ không được hiện như sự thật.
Badge chỉ đọc heartbeat tên `review-worker` (worker trên Mac). Một máy render Windows
([windows-render-worker](windows-render-worker.md)) ghi heartbeat riêng tên `render-worker-win`,
chưa hiện trên dashboard; xem bằng `select name, seen_at from localization.host_workers`.

## Giới hạn

- Không hủy một run localization (dùng Temporal UI); downloader giữ Temporal chỉ đọc.
- "Chạy lại" luôn là một generation mới chạy từ đầu; muốn giữ các bước đã xong thì dùng "Chạy tiếp từ bước lỗi" (chỉ cho run đã lỗi ở một activity). Reset chạy lại cả các activity chạy song song với bước lỗi mà xong sau nó.
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

`scripts/start-all.sh` khởi động Docker và bốn tiến trình pm2 (`reup-voice-worker`,
`reup-ingest-worker`, `reup-review-auto-open`, `reup-review-worker`). Đổi code `nest` hoặc `web` thì build lại image
(`docker compose up -d --build --no-deps nest web`). Chi tiết worker review, Chrome và render:
[review-companion](review-companion.md). Màn hình Workspace 2D đã gỡ; xem
[agent-workspace](agent-workspace.md).

## Tách nhạc trên GPU (`reup-ingest-worker`)

Bước `ingest` (Demucs tách giọng khỏi nhạc) chạy ở một worker pm2 trên máy, queue `ingest`, dùng GPU
Apple (MPS). Trong container nó chỉ có CPU của máy ảo Docker (5 nhân, chia với mọi việc khác), và một
video 6,6 phút từng mất hơn 16 phút; trên host đo được khoảng 2,3 đến 2,6 lần nhanh hơn CPU host, kết
quả giống nhau (SNR 98 đến 100 dB khi tắt dịch ngẫu nhiên).

- Cài một lần: `scripts/setup-host-demucs.sh` (Python riêng với torch 2.6.0, vì torch 2.5.1 của dự án
  không chạy Demucs trên MPS; trọng số lấy theo URL và sha256 trong Dockerfile). Kết quả nằm ở
  `services/localization/.host-demucs/`, không vào git.
- Chạy: `scripts/start-all.sh` đã gồm nó và chờ tới khi nó báo đang phục vụ queue `ingest`. Nếu MPS
  lỗi, lệnh `demucs-host` tự chạy lại bằng CPU của host.
- Tắt `reup-ingest-worker` thì bước tách nhạc của run mới **chờ mãi** (như `stt` và `tts` khi tắt
  `reup-voice-worker`) và badge "Tách nhạc (GPU)" đỏ. Run bắt đầu trước khi có queue này vẫn gửi
  `ingest` sang queue `media`, nên container `worker-media` vẫn phục vụ `ingest` cho chúng.

## Chạy tuần tự các video đang giữ

Video `held` được thả vào localization từng cái một, và không có tiến trình nào polling: migration
`261113-sequential-queue` đặt trigger trong Postgres. Khi không còn video nào do hàng đợi này thả mà
chưa xong (`auto_queue_inflight` trống), video cũ nhất (id nhỏ nhất, tức tập đầu của một series)
chuyển từ `held` sang `pending`, ghi vào log hành động với `source: auto_queue`, và starter nhận
nó như một video bình thường. Hàng đợi tiếp tục khi một trong các sự kiện sau xảy ra cho video đang chạy:

- `publish` ghi dòng `localization.runs` (mọi kết quả: chờ duyệt, cần người xem, **lỗi**, bỏ qua), nên
  video lỗi không chặn video sau;
- dòng outbox thành `rejected` hoặc `dead`, tức không bao giờ tới được workflow;
- một dòng outbox mới thành `held` khi hàng đợi đang trống (video đầu tiên, hoặc video vừa tải xong).

Hệ quả: kênh tắt "Tự localize" vẫn có video `held`, nhưng chúng sẽ lần lượt chạy; video bạn tự bấm
"Đưa vào localization" chạy song song bên cạnh và không làm hàng đợi tiến lên. Một workflow chết mà
không ghi `runs` (quá hạn 14 ngày, bị hủy trong Temporal) làm hàng đợi đứng; sự kiện tiếp theo sau 14
ngày tự gỡ, còn muốn chạy ngay thì chạy
`DELETE FROM auto_queue_inflight; SELECT auto_queue_release_next();`.

### Bật/tắt hàng đợi

Công tắc "Tự chạy video đang giữ khi video trước tới bước chờ duyệt" ở đầu dashboard (migration
`261114-auto-queue-switch`, bảng `auto_queue_settings`, API `GET`/`PUT /api/dashboard/auto-queue`):

- **Tắt:** video đang chạy vẫn chạy tới hết, nhưng khi nó xong thì không thả video nào; video `held`
  mới cũng đứng yên.
- **Bật:** nếu không có video nào của hàng đợi đang chạy, video `held` cũ nhất được thả ngay; sau đó
  hàng đợi chạy như trên.

Dùng công tắc thay cho việc gỡ trigger. Gỡ trigger bằng tay (chạy file `.down.sql` mà không xóa dòng
trong `applied_migrations`) làm `apply-migrations.sh` tưởng hàng đợi vẫn còn. Đó là tình trạng đã gặp
từ 2026-11-13 tới 2026-11-14; lúc sửa, công tắc được đặt ở trạng thái tắt.

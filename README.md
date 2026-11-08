# reup-flow (LinguaFlow)

Tải video Douyin, dịch và lồng tiếng Việt bằng các agent, tự chấm trước khi đăng. Trạng thái
workflow nằm ở Temporal, trace và eval nằm ở Langfuse.

## Hệ thống gồm những gì

| Thành phần | Chạy ở đâu | Vai trò |
|---|---|---|
| `nest` (services/downloader) | compose, cổng 3000 | API, tải video, hàng đợi |
| `parser` (submodule `src/Douyin_TikTok_Download_API`) | compose, nội bộ | gọi Douyin, không mở cổng ra ngoài |
| `web` (services/web) | compose, cổng 5173 | giao diện và trang Agent Workspace |
| `localization-starter` | compose | biến hàng outbox thành workflow |
| `worker-agents` | compose | Localizer, Dubber, Reviewer, OCR, gọi STT/TTS (queue `workflows`, `llm`, `io`, `voice_check`, `stt`, `tts`) |
| `worker-media` | compose | Demucs và ffmpeg (queue `media`, `render`), nặng CPU, RAM, đĩa |
| Temporal, Postgres, Redis, MinIO | compose | điều phối, dữ liệu, cache cookie, lưu media |
| Langfuse (web, worker, ClickHouse) | compose, cổng 3100 | trace và eval |
| **VoiceStudio** | **máy GPU riêng** | STT và TTS qua HTTP; **không nằm trong repo này** |

## Yêu cầu máy mới

- Docker với Compose v2, `git`, `curl`, `openssl`.
- Khoảng 30 GB đĩa trống, khuyến nghị 8 GB RAM trở lên (ClickHouse giới hạn 2 GB, Demucs cần thêm).
- Truy cập được tới VoiceStudio (URL, API key, voice id, model STT và TTS) và một key OpenRouter.
- Cookie Douyin để tải video (đẩy bằng extension Chrome trong `services/extension`, hoặc đặt
  `DOUYIN_COOKIE_FALLBACK` trong `.env`).

## Cách 1: dựng mới (không mang dữ liệu cũ)

```bash
git clone --recurse-submodules https://github.com/rexviet/reup-flow.git
cd reup-flow
git checkout feat/localization-dubber-code-mode   # nhánh đang làm việc
scripts/bootstrap.sh --host <tên-hoặc-IP-của-máy-này>
```

`--host` là địa chỉ mà trình duyệt ở máy khác dùng để vào máy này (giao diện web gọi API ở
`http://<host>:3000`). Bỏ qua nếu chỉ dùng tại chỗ.

`bootstrap.sh` làm lần lượt: kiểm tra công cụ và đĩa, lấy submodule, tạo `.env` với secret sinh
ngẫu nhiên (nếu chưa có), khởi động Postgres, tạo role, **áp migration**, build và khởi động toàn
bộ, chờ các service khỏe, rồi kiểm tra từng endpoint. Cuối cùng nó liệt kê các giá trị còn trống.

Compose không tự áp migration, nên đừng chạy `docker compose up` trơn trên máy trống: các route
chạm database sẽ lỗi 500. `scripts/apply-migrations.sh` ghi lại file nào đã áp, nên chạy lại an toàn.

Điền các giá trị còn trống vào `.env`, rồi `docker compose up -d` để tạo lại service đã đổi:

```
OPENROUTER_API_KEY  LOCALIZER_MODEL  DUBBER_MODEL  OCR_MODEL  OCR_BBOX_FORMAT  REVIEWER_MODEL
VOICESTUDIO_URL  VOICESTUDIO_API_KEY  VOICE_ID  STT_MODEL  TTS_MODEL
```

Ghi chú: `VOICESTUDIO_URL` dạng `https://….trycloudflare.com` đổi mỗi lần tunnel khởi động lại. Khi đổi
URL, phải khởi động lại cả `localization-starter` và `worker-agents`
(`docker compose up -d localization-starter worker-agents`), nếu không outbox sẽ đứng im mà không báo lỗi.

## Cách 2: mang cả dữ liệu từ máy cũ

Dùng khi muốn giữ lịch sử Temporal, video, và các lần chạy eval trên Langfuse.

```bash
# máy cũ
scripts/state-pack.sh export /duong/dan/pack      # dừng stack, đóng gói 3 volume, .env, data/douyin

# chuyển thư mục pack sang máy mới bằng kênh an toàn (scp, ổ cứng ngoài…)

# máy mới
git clone --recurse-submodules https://github.com/rexviet/reup-flow.git && cd reup-flow
scripts/state-pack.sh import /duong/dan/pack
scripts/bootstrap.sh --host <tên-hoặc-IP>
```

Gói chứa toàn bộ secret trong `.env` và nội dung database: không commit, không gửi qua kênh công
khai. Hai máy phải cùng kiến trúc CPU (arm64 với arm64, hoặc x86_64 với x86_64), vì volume được
chép nguyên file. Script từ chối chạy nếu kiến trúc khác nhau hoặc checksum sai.

## Chạy một video thật từ đầu đến cuối

```bash
scripts/e2e-smoke.sh "https://www.douyin.com/video/<id>"  [số-phút-tối-đa]
```

Script đưa video vào qua `POST /api/videos`, rồi theo dõi `localization.runs` tới khi có trạng thái
cuối. Thoát 0 chỉ khi video là `published`; `published_original`, `skipped_ad`, `skipped_no_video`,
`needs_human`, `failed` đều thoát 1 và nói rõ trạng thái. Theo dõi trực quan ở:

- Agent Workspace: `http://<host>:5173` (API key được đóng sẵn vào bản build của web)
- Temporal UI: `http://localhost:8233`
- Langfuse: `http://localhost:3100` (tài khoản và mật khẩu trong `.env`)

Temporal UI, Langfuse và MinIO console chỉ nghe ở `127.0.0.1` theo thiết kế. Để mở từ máy khác, dùng
SSH tunnel, ví dụ `ssh -L 3100:127.0.0.1:3100 user@máy-mới`.

## Tách thành nhiều máy

Các worker chỉ cần địa chỉ Temporal, nên có thể chạy `worker-media` hoặc `worker-agents` ở máy
khác. Hiện compose **không** mở cổng Temporal (7233) ra ngoài, và worker cần cả địa chỉ MinIO,
Langfuse và Postgres. Muốn tách máy thì phải mở các cổng đó qua mạng riêng (Tailscale là lựa chọn
mà `VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS` đã giả định) và sửa compose cho phù hợp. Phần này chưa có
sẵn script.

## Khi có sự cố

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| Route API trả 500 trên máy mới | chưa áp migration: chạy `scripts/apply-migrations.sh` |
| Outbox có hàng `pending` nhưng không workflow nào chạy | URL VoiceStudio cũ: sửa `.env`, khởi động lại `localization-starter` và `worker-agents`; starter từ chối gửi khi `VOICE_ID` không có trong `GET /v1/audio/voices` |
| Tải video lỗi 400 không rõ lý do | cookie Douyin hết hạn hoặc thiếu `UIFID`: đẩy lại cookie bằng extension |
| `nest` không khởi động | thiếu biến bắt buộc trong `.env`, hoặc `API_KEY` ngắn hơn 16 ký tự |
| Build lần đầu rất lâu | image `worker-*` kéo theo Demucs và PyTorch; các lần sau dùng cache |

Log từng service: `docker compose logs -f <service>`.

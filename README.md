# reup-flow (LinguaFlow)

Tải video Douyin, dịch và lồng tiếng Việt bằng các agent, tự chấm trước khi đăng. Trạng thái
workflow nằm ở Temporal, trace và eval nằm ở Langfuse.

## Hệ thống gồm những gì

| Thành phần | Chạy ở đâu | Vai trò |
|---|---|---|
| `nest` (services/downloader) | compose, `127.0.0.1:3300` (3000 trong container) | API, tải video, hàng đợi |
| `parser` (submodule `src/Douyin_TikTok_Download_API`) | compose, nội bộ | gọi Douyin, không mở cổng ra ngoài |
| `web` (services/web) | compose, `127.0.0.1:5173` | dashboard quản trị: sức khỏe, cần xử lý, pipeline, chi tiết video, kênh và job tải |
| `localization-starter` | compose | biến hàng outbox thành workflow |
| `worker-agents` | compose | Localizer, Dubber, Reviewer, OCR, gọi STT/TTS (queue `workflows`, `llm`, `io`, `voice_check`, `stt`, `tts`) |
| `worker-media` | compose | Demucs và ffmpeg (queue `media`, `render`), nặng CPU, RAM, đĩa |
| `review-bridge` | compose tùy chọn, loopback 3310 | companion sửa phụ đề/SRT và yêu cầu revoice; chưa rollout |
| Temporal, Postgres, Redis, MinIO | compose | điều phối, dữ liệu, cache cookie, lưu media |
| Langfuse (web, worker, ClickHouse) | compose, cổng 3100 | trace và eval |
| `reup-review-worker`, `reup-voice-worker`, `reup-review-auto-open` | pm2 trên host | render bản cuối sau khi duyệt, mở Studio theo yêu cầu dashboard, heartbeat; STT/TTS; tự mở Studio ([dashboard](docs/admin-dashboard.md), [review](docs/review-companion.md)) |
| **VoiceStudio** | **máy GPU riêng** | STT và TTS qua HTTP; **không nằm trong repo này** |

## Nền phụ đề

Bản layout mới chỉ hiện hộp nền trong đúng các frame có caption Việt; khoảng
trống giữa caption giữ nguyên hình gốc. Quy tắc nằm trong
[`contracts/edit-spec.schema.json`](contracts/edit-spec.schema.json):
`cover.only_with_captions=true`. Spec cũ thiếu cờ này giữ cách render và hash cũ;
layout mới tạo hash khác để không lấy nhầm video đã cache với hộp nền suốt video.

## Ngôn ngữ nguồn

Workflow VoiceStudio chia audio đã tách giọng theo Silero VAD thành các đoạn
không chồng nhau, tối đa 12 giây, với 200 ms đệm ở biên. Mỗi đoạn gọi STT với
`temperature=0` và tự nhận ngôn ngữ (cùng `multilingual=true` khi `STT_MULTILINGUAL=true`);
timestamp được ghép về timeline gốc trước khi dịch. Không có speech thì không gọi STT.

### Engine STT: MLX Whisper trên GPU Apple

Mặc định VoiceStudio chạy **MLX Whisper** (`whisper-large-v3-mlx`) thay cho faster-whisper trên CPU:
nhanh hơn khoảng 4 lần và không còn chuỗi lặp ở đoạn có tiếng ngân hoặc nhạc nền. Cấu hình gồm:

- `scripts/launch-voicestudio.py` mở app với `OMNIVOICE_ASR_BACKEND=mlx-whisper` và
  `OMNIVOICE_MLX_FORCED_ALIGN=0`, qua `OMNIVOICE_BACKEND_CMD` để app không chép đè runtime.
  Nó vá `backend/services/asr_backend.py` (xem `scripts/voicestudio-patches/`) chỉ khi file trùng
  checksum bản gốc trong app. Tắt căn từ wav2vec2 vì mốc từ quá chặt làm lộ khoảng lặng trong câu hát,
  `SILENCE_GAP_MS` cắt một câu hát thành nhiều segment và `detect_singing` bỏ sót lời bài hát.
  `--check` chỉ kiểm tra, `--reset` trả file về bản gốc. `scripts/start-all.sh up` tự gọi nó khi
  VoiceStudio chưa trả lời; app đang chạy thì thoát trước khi muốn áp thay đổi.
- `ecosystem.config.cjs` đặt `STT_MODEL=mlx-whisper` và `STT_MULTILINGUAL=false` cho
  `reup-voice-worker`. MLX không có `multilingual`, nên mỗi đoạn tự nhận ngôn ngữ riêng; lời pha hai
  ngôn ngữ trong một đoạn có thể chỉ được nhận một ngôn ngữ.
- Lần đầu cần tải model (2,9 GB) vào cache Hugging Face; nếu chưa có, request đầu tiên sẽ tự tải
  và có thể quá thời gian.

Muốn quay lại faster-whisper: thoát app, `scripts/launch-voicestudio.py --reset`, mở app bình thường,
xóa khối `env` của `reup-voice-worker` trong `ecosystem.config.cjs`, rồi `scripts/start-all.sh reload`.

Phần dưới mô tả chế độ `multilingual` của faster-whisper (`STT_MULTILINGUAL=true`).

Transcript lưu `segments[].language` theo kết quả nhận dạng của từng chunk.
Nếu các chunk có nhiều ngôn ngữ, `language` của toàn file là `null`; đây là
nhãn do model đoán, không phải ngôn ngữ đã được xác minh cho từng từ.
Localizer ở cả chế độ single và tool nhận nhãn này, dịch nguồn nhiều ngôn ngữ
sang Việt và không mặc định mọi đoạn là tiếng Trung. Transcript cũ thiếu nhãn
vẫn đọc được. Các tên trường cũ như `src_zh` được giữ để tương thích dữ liệu.

API VoiceStudio phải expose trường multipart `multilingual` trên
`POST /v1/audio/transcriptions` và chuyển tới faster-whisper. Client kiểm tra
`/openapi.json` trước mỗi đoạn; API chưa hỗ trợ sẽ làm bước STT báo lỗi rõ ràng,
không âm thầm chạy theo cách cũ. Không dùng đồng thời với `language` cố định.
Runtime local hiện đã có bản vá; source VoiceStudio sẽ được tích hợp riêng sau.
Không cần thêm biến môi trường. Vbee và provider `openai_compat` giữ cách chạy cũ.

Cách này áp dụng cho các bước STT chạy mới, không tự dựng lại output đã hoàn tất.
Input `review_project` đã có Localizer theo ngữ cảnh; xem [runbook review](docs/hyperframes-review.md).
STT vẫn có thể sai và bản dịch cần được kiểm tra.

Chạy A/B với runtime đã vá, dùng thư mục kết quả mới:

```bash
python3 scripts/spike-voicestudio-multilingual.py \
  --input data/diagnostics/multilingual-spike/vocals-16k.wav \
  --output-dir data/diagnostics/multilingual-spike-new
```

Bản vá local và bản gốc nằm trong
`backups/voicestudio-multilingual-spike-20261108-1944/` (không commit).
VoiceStudio mở bình thường sẽ chép đè runtime. Để chạy lại spike, thoát app rồi
chạy `python3 backups/voicestudio-multilingual-spike-20261108-1944/launch-runtime-spike.py`.
Launcher kiểm tra checksum, dùng override `OMNIVOICE_BACKEND_CMD` với `--app-dir`
tuyệt đối để app tự quản lý backend; `--check` chỉ kiểm tra trạng thái.
Để hoàn tác, thoát app, khôi phục bốn file theo `manifest.json`, xóa test spike
`backend/tests/test_openai_multilingual_spike.py` trong runtime và mở app bình thường.

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

### Cổng trên máy local này

Máy này đã có GoClaw dùng cổng 3000 và 5432. Bản setup local dùng API
`http://localhost:3300`, Postgres `localhost:5433`, và giữ web tại
`http://localhost:5173`. Kết nối giữa các container vẫn dùng cổng gốc.
Image nền worker lấy từ `docker.io/astral/uv` vì Docker trên máy này bị GHCR
trả 403; SHA-256 giữ nguyên như bản ghim upstream.

`PROBE_SEC_USER_ID` được lấy từ profile công khai trong fixture
`services/downloader/test/fixtures/aweme/video-7481199234537033018.json` để API
khởi động; probe trực tiếp và tải video chưa được xác minh. Cookie Douyin đã được
đẩy thành công qua extension và endpoint trạng thái xác nhận `present: true`.
Database được sao lưu trước migration vào `backups/pre-bootstrap-*.sql` (không commit).

VoiceStudio đang chạy trên máy Mac ở cổng 3900. Các container truy cập qua
`VOICESTUDIO_URL=http://192.168.65.2:3900`; IP này là địa chỉ
`host.docker.internal` trong Docker Desktop hiện tại. Cấu hình
`VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS` giữ dải Tailscale và thêm đúng
`192.168.65.2/32` để cho phép HTTP tới máy host. Nếu Docker Desktop đổi địa chỉ
host, phải xác định lại IP và cập nhật cả hai biến trước khi tạo lại service.

Hạ tầng, web, API, starter và các worker đã chạy. STT/TTS model đã có trên
VoiceStudio local. `VOICE_ID` đã chọn voice `ngoc-huyen` và được xác minh qua
`/v1/audio/voices`. Sau khi thay đổi cấu hình provider, tạo lại hai service và
chạy E2E:

```bash
cd ~/Data/reup-flow
docker compose up -d localization-starter worker-agents
scripts/e2e-smoke.sh "https://www.douyin.com/video/<id>"
```

### Chạy cả stack (Docker + tiến trình host qua pm2)

```bash
scripts/start-all.sh            # start: Docker, rồi các tiến trình host dưới pm2
scripts/start-all.sh status     # Docker, pm2 và VoiceStudio có trả lời không
scripts/start-all.sh reload     # reload các tiến trình host sau khi đổi code worker
scripts/start-all.sh stop
```

Ba tiến trình host chạy dưới pm2 ([ecosystem.config.cjs](ecosystem.config.cjs)):
`reup-voice-worker` (hàng đợi `stt` và `tts`), `reup-review-auto-open` (tự mở Studio khi Studio đang trống)
và `reup-review-worker` (render bản đã duyệt, quét review, mở Studio theo yêu cầu dashboard, heartbeat).
Xem log và điều khiển từng cái: `pm2 logs reup-voice-worker`, `pm2 reload <tên>`,
`pm2 stop <tên>`. Muốn tự chạy lại sau khi khởi động máy: `pm2 save` rồi `pm2 startup`.

Xem Studio từ ngoài LAN khi router không forward cổng: `scripts/remote-studio.sh start --key "<public key>"`
chạy một sshd riêng (không cần root, cổng 2222 trên loopback) và một tunnel `ngrok tcp`. sshd này chỉ
nhận khóa công khai bạn đưa, không có shell, và chỉ chuyển được tới Studio (`3002`) và trang review
(`3310`); sshd hệ thống ở cổng 22 không bị lộ. `scripts/remote-studio.sh url` in lệnh `ssh -N -L`
và cách lấy link review (một `ssh` không có `-N` in link với grant mới). Xong việc thì
`scripts/remote-studio.sh stop`. Địa chỉ ngrok miễn phí đổi mỗi lần start, và đừng `pm2 save` khi tunnel đang chạy.

Worker `voice` chạy trên host vì VoiceStudio chỉ nghe ở `127.0.0.1`, còn từ container
chỉ vào được qua cổng host của Docker Desktop, và đường đó làm rớt khoảng 45% kết nối mới
(đo 12 phút: 111/240 lần lỗi từ container, 0/240 từ host). Compose override đặt
`VOICE_QUEUES_ON_HOST=true` cho `worker-agents` và mở Temporal (`127.0.0.1:7233`) và MinIO
(`127.0.0.1:9000`) chỉ trên loopback cho worker host. Worker host lấy cấu hình từ
`docker compose config` của `worker-agents`, chỉ đổi các địa chỉ nội bộ Docker sang loopback;
không chép khóa vào file. Khi `VOICE_QUEUES_ON_HOST=true` mà worker host không chạy, tác vụ
STT/TTS nằm chờ trong Temporal đến khi nó chạy lại.

`REVIEWER_MODE=jev` đang dùng mặc định nên `REVIEWER_MODEL` chỉ cần khi đổi sang
`REVIEWER_MODE=llm`. `DUBBER_MODE=code` đã được bootstrap cấu hình.

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

Script đưa video vào qua `POST /api/videos` với `localize: true` (video không có cờ này bị giữ ở trạng thái `held`), rồi theo dõi `localization.runs` tới khi có trạng thái
cuối. Mặc định mới đòi `ready_for_review`, bundle hợp lệ và không có output; `needs_human`
có project hợp lệ thoát 4 để yêu cầu người sửa. Khi chạy pipeline legacy hiện tại, dùng
`--mode legacy_render` để tiếp tục đòi `published`. [Runbook review](docs/hyperframes-review.md)
ghi đầy đủ exit codes, canary, migration và rollback. Theo dõi trực quan ở:

- Dashboard ([hướng dẫn](docs/admin-dashboard.md)): `http://localhost:5173` (API key được đóng sẵn vào bản build của web; web gọi API cùng origin qua nginx)
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

## Thử nghiệm HyperFrames review

Spike tương thích Studio, SRT và revoice chạy local được mô tả trong
[HyperFrames compatibility spike](docs/hyperframes-review-spike.md). Host launcher
đã có boundary guard và mở được full video canary; các gate round-trip còn lại vẫn được theo dõi.

Localizer đã có chính sách ngữ cảnh cho input `delivery_mode: review_project`;
run mới còn đối chiếu ASR với chữ gốc đọc từ frame theo timestamp, đưa cùng bằng
chứng vào lượt dịch và lượt sửa; xem [runbook](docs/hyperframes-review.md).
Input cũ vẫn mặc định `legacy_render`; Mac hiện tại bật review bằng Compose override. Câu `inferred`
giữ bản dịch cùng lý do để review; câu `unresolved` giữ ID/timing/lý do nhưng không
gọi TTS hoặc tạo caption. Audio dùng nhạc nền hoặc im lặng, không dùng giọng gốc.
Các trường dữ liệu nằm trong [contracts](services/localization/src/localization/contracts/models.py).
Backend review đã có bundle bền vững: `project_key` trỏ tới
`review/{aweme}/g{generation}/head.json`; HEAD chọn manifest revision bất biến,
kèm các asset có checksum trong cùng generation. Mỗi generation capture source
trước prep và dùng work keys riêng. Bundle gồm video mute, một track audio mixed,
cue clips, font, caption/SRT, script và thông tin voice/model/engine để review/revoice.
`materialize_project` trong [review_project.py](services/localization/src/localization/activities/review_project.py)
khôi phục đủ dependency từ HEAD thành folder tự chứa.

Starter đọc `LOCALIZATION_DELIVERY_MODE` cho mỗi dispatch VoiceStudio mới. Base Compose
giữ `legacy_render`; [override trên Mac hiện tại](docker-compose.override.yml) chọn
`review_project`. Input `review_project` cùng marker workflow mới kết thúc ở
`ready_for_review` hoặc `needs_human`, giữ `project_key` và không render MP4 hay ghi
`output/`. Reviewer cần frame hoặc không khả dụng sẽ chuyển sang `needs_human`.
Vbee, input/history cũ và các early gate giữ hành vi trước đây. Rollback mode chỉ
ảnh hưởng dispatch mới; phải giữ worker hiểu cả histories mới.

Delivery review đã bật cho dispatch mới trên Mac hiện tại theo yêu cầu người dùng. [Migration runner](scripts/apply-migrations.sh) đã có
backup/restore verification và atomic ledger; nullable `project_key` đã được áp sau
backup và thử restore. Worker/starter đang hỗ trợ review; run đã start giữ mode đã ghi. LaunchAgent tự mở
Studio/companion cho project mới ở `ready_for_review` hoặc `needs_human`, chờ khi
Studio đang review project khác. [Runbook rollout](docs/hyperframes-review.md)
sở hữu thứ tự upgrade, canary một generation, activation và rollback. Companion/launcher và revoice đã có
implementation cùng kiểm tra local; [hướng dẫn companion](docs/review-companion.md)
ghi cấu hình, phục hồi, gate live riêng và giới hạn hiện tại. Studio compatibility
gates còn lại chưa được coi là đã nghiệm thu khi bật cấu hình local này.

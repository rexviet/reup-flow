# Companion review và revoice

Companion lưu phụ đề/timing/SRT và tạo lại giọng từng cue. Studio chỉnh visual theo
stable cue ID. HEAD trong MinIO là bản đã commit; thư mục `data/review` là cache
để Studio mở. Caption mới có thể khác giọng đang phát: giao diện luôn báo mismatch.
Lưu phụ đề không gọi TTS. Caption trống vẫn giữ hàng review và audio active cũ,
nhưng không xuất cue SRT.

Launcher dùng [boundary preload](../scripts/studio-boundary.mjs) để bind loopback
và kiểm exact Host/Origin trước vendor handlers, kể cả websocket upgrade. Probe
project rỗng và project thật vẫn bắt buộc; pin không đổi. Canary đã mở/phát full
video trong Studio; [bằng chứng](../plans/reports/ops-261110-1916-native-studio-full-video.md).
Round-trip style và các acceptance rollout còn lại vẫn mở.

## Cấu hình sau khi gate đạt

Cần macOS, Docker Compose, Node **22.13 trở lên**, và package Studio đúng lockfile:

```sh
cd services/localization/spikes/hyperframes-review
pnpm install --frozen-lockfile
```

Bridge dùng hai biến `REVIEW_S3_ACCESS_KEY_ID` và `REVIEW_S3_SECRET_ACCESS_KEY`
riêng trong nguồn secret hiện có của dự án. Không dùng identity worker, downloader,
Langfuse hoặc MinIO root. [MinIO init](../infra/minio/init.sh) tạo policy Get/Put
và prefix-scoped List cho `review/`; không cấp Delete hay đọc source/work/cache/output.
Identity mới chỉ được tạo nếu cả hai biến đã có. Mac canary đã provision identity
riêng từ nguồn secret hiện có, với sự cho phép của người dùng.

Bridge đọc riêng `BridgeSettings`, không dùng cấu hình provider/DB của worker.
Service và mount là nguồn chính xác trong [Compose](../docker-compose.yml).
Bridge listen trong container nhưng chỉ publish `127.0.0.1:3310`. Temporal vẫn
nội bộ. Không có Docker socket trong container; CLI bên trong bridge chỉ trả dữ
liệu và launch grant qua pipe của host launcher.

Khi đã có identity đúng policy, worker mới và review project thử nghiệm:

```sh
mkdir -p data/review
docker compose --profile review up -d --build review-bridge
node scripts/review-studio.mjs open <aweme> <generation>
# Giữ launcher ở foreground. Ctrl+C dừng Studio mà nó khởi chạy.
# Từ terminal khác, cùng Node đáp ứng version:
node scripts/review-studio.mjs stop
```

Studio dùng port cố định 3002. Nếu port bận, launcher nêu owner và dừng; không đổi
port hoặc kill process ngoài record. Reuse chỉ áp dụng đúng PID/command/project.
Lock còn lại sau crash được reclaim khi PID/command cũ không còn khớp.

Nếu preview quay mãi dù timeline/sub đã hiện, kiểm tra Network trước khi restart
workflow. File video có thể trả Range 206 nhanh trong khi Chrome giữ request ở
Stalled. Giữ một tab Studio đang dùng, đóng các tab Studio trùng do các lần launch
trước rồi reload tab hiện tại; các thay đổi source đã lưu vẫn nằm trong project.
Không đóng tab đang có nội dung chưa lưu. Lần kiểm tra thực tế được ghi trong
[báo cáo preview](../plans/reports/fix-261110-2042-studio-preview-stall.md).

## Tự mở trên Mac hiện tại

[Compose override](../docker-compose.override.yml) đặt starter thành `review_project`.
Video mới tải qua extension được dispatch theo mode này; run đã ghi input giữ mode cũ.
Không sửa hoặc chạy lại video cũ khi bật watcher.

```sh
# Sau khi worker/bridge và canary đã sẵn sàng:
docker compose build localization-starter
docker compose up -d --no-deps --force-recreate localization-starter
docker compose --profile review up -d --no-deps review-bridge
python3 scripts/install-review-auto-open.py install
```

`scripts/start-all.sh` thay cho bước cài LaunchAgent: nó chạy watcher dưới pm2
(`reup-review-auto-open`) và tự dừng LaunchAgent nếu còn, để không có hai watcher cùng mở
một project. State và log tự mở vẫn nằm trong `data/review-launcher/`.

Installer cần Node 22.13+ và Docker trên PATH. Nó cài LaunchAgent
`com.reup-flow.review-auto-open` cho tài khoản Mac đang đăng nhập, tự chạy khi login.
Watcher mỗi 5 giây đọc danh tính project hoàn tất qua CLI read-only trong starter;
không mang credential DB/API ra host. Cả `ready_for_review` và `needs_human` có HEAD
đúng generation đều được mở bằng launcher có boundary gate hiện tại.

Chỉ nhận kết quả từ lần bật đầu tiên. Mốc thời gian, danh sách đã mở và log nằm
trong `data/review-launcher/auto-open*`, giữ qua restart. Khi Studio đang mở project
khác, project mới chờ; không ngắt review hoặc ghi đè local edits. Review xong, chạy
`node scripts/review-studio.mjs stop` để nhường Studio cho project tiếp theo.
Đóng tab browser không dừng server Studio. Launch lỗi sẽ retry, không đánh dấu đã mở.

Dừng tự mở: `python3 scripts/install-review-auto-open.py stop`. Lệnh này giữ state
và các file review. Rollback dispatch bằng cách đặt mode trong Compose override về
`legacy_render`, rồi recreate riêng starter; run đã start vẫn giữ mode đã ghi.

## Lưu và phục hồi

Native preview dùng runtime tại origin Studio cố định trong CSP. Element có
editor ID ổn định; preview, waveform, thumbnail cache và stamp metadata không
được tính là source edits. HTML/CSS/JSON nguồn và overrides vẫn được theo dõi.

Box mặc định trắng ở alpha 0.25, hiện suốt video, rộng 60% và căn giữa (20% mỗi
bên). Font là 20px cho khung cao 576px và tỉ lệ theo chiều cao khung (khung 1080px: 38px), để chữ trông cùng độ lớn ở mọi video. Generator đo bằng font đi kèm, xuống dòng và nới box khi
câu dài; không tự thu nhỏ chữ. Box chỉ nới trong thời gian câu dài, rồi về 60%; các
khoảng không có sub vẫn có box, không fade hoặc biến mất.
Line-height là 28px và viền chữ 4px ở font 20px, cùng tỉ lệ 1,4 và 0,2 lần cỡ chữ ở khung khác (1080px: 53px và 8px).
Viền chữ là `-webkit-text-stroke` (đơn vị px theo tỉ lệ trên, 4px ở font 20px) trong `.caption` ở Code → `index.html`.
Stroke width trong panel Style của Studio là viền element, không phải viền glyph.

- Chỉnh text/timing rồi chọn **Lưu phụ đề**. Timing phải nằm trong video và không
  chồng neighbor. Audio active giữ nguyên placement nếu slot mới không vừa.
- Visual import nhận các thuộc tính cue trong
  [`visual.py`](../services/localization/src/localization/review_bridge/visual.py).
  Đổi số từ ở cue sớm không đổi style cue sau. Các file Studio HTML/CSS/JSON được
  lưu thành asset bất biến. Source edit ngoài importer trả conflict và giữ file;
  không âm thầm regenerate đè lên edit chưa hiểu. Native override round-trip
  vẫn phải được chứng minh bằng gate Studio.
- **Tạo lại giọng** chỉ dùng caption đã lưu. Worker dùng `Synthesizer` và
  `TtsByProvider`/`ConfiguredVoices` hiện có. Clip dài tối đa 1,15 lần slot được
  phát qua `atempo` thật. Dài hơn hoặc provider lỗi thì giữ giọng cũ và caption mới.
  Worker dựng lại một mixed track bằng clip mới, các clip cũ và BGM; video gốc mute.
- Revoice dùng requestId cố định, workflow ID dẫn xuất và request/result refs bất
  biến. Reload hoặc mất response attach cùng job; worker commit bằng HEAD ETag.
  Kết quả stale giữ candidate và báo conflict, không tự rebase.
- Queue có tối đa 16 reservation chưa có outcome, kiểm soát bằng S3 CAS, không dựa
  vào Temporal visibility. HTTP 429 giữ request để retry. Receipt hoàn tất giải
  phóng slot khi nhận request kế tiếp. Reservation chưa start sau crash được nối
  lại bằng cùng request; không tự expire một job có thể đã start.
- Materialize đọc HEAD và kiểm checksum. Cache sạch có thể phục hồi sau crash hoặc
  bị xóa. Journal local cho phép resume refresh dở dang; file Studio thay đổi ngoài
  transaction làm refresh dừng. Không xóa orphan/candidate để xử lý incident.
- Reload companion giữ bản nhập chưa lưu của các hàng. Sau revoice thành công,
  tải lại preview native Studio để nghe mixed track mới.

Grant dùng một lần trong 60 giây, chỉ có trong pipe/URL fragment và bị xóa khỏi
address bar trước POST redeem. Session HttpOnly/SameSite Strict tối đa 8 giờ,
rotate mỗi lần launch. API kiểm exact Host/Origin và CSRF. Không log grant/cookie;
không chạy lệnh process listing có thể in URL fragment của browser/launcher.

Rollback: dừng nhận request mới ở bridge; giữ worker xử lý job đã start. CAS HEAD
về manifest đã chọn bằng **ETag hiện tại**, rồi rematerialize cache sạch. Giữ
revision, clip và request outcomes để điều tra; không rollback bằng local pointer.

## Export MP4 trong Studio

Mở tab **Renders**, chọn MP4, độ phân giải, FPS và chất lượng rồi bấm **Export**.
Bản review dùng runtime HyperFrames quản lý media và caption theo timeline;
không có player tự chạy song song bằng requestAnimationFrame. Font, box và các
visual edits đã lưu trong project được dùng khi export. File xuất local không tự
publish và không thay đổi trạng thái workflow.

CSP chỉ cho phép các hash runtime/bridge export của HyperFrames 0.8.77 đã xác minh,
bao gồm FPS 24/30/60 của Studio. Runtime export có một newline cuối khác runtime
preview; không dùng hash của preview để suy đoán export đã được phép. Hash và
nguồn kiểm chứng nằm trong [runtime-hashes.json](../services/localization/spikes/hyperframes-review/runtime-hashes.json).
Compiler có thể nhúng font/ảnh dạng data URI; CSP cho phép đúng hai loại tài nguyên
đó, không cho phép script tùy ý hoặc CDN. Khi đổi pin/runtime/config render seek,
phải xác minh lại các byte script trước khi cập nhật hash.

Projection chỉ có một root `index.html`. Bản HTML asset bất biến được giữ bằng
đuôi `.html.blob` để Studio không nhận nhầm thành composition khác. Các bản cũ
chỉ được đổi tên khi checksum khớp tên asset; file bị sửa tay được giữ và báo lỗi.
History trong object store vẫn nguyên. Dùng launcher để mở project; không mở
các bản HTML asset như một composition độc lập.

## Render bản cuối trên máy (`reup-review-worker`)

Worker pm2 `reup-review-worker` (`scripts/review-worker.mjs`, chạy bởi `scripts/start-all.sh`)
làm bốn việc mỗi vòng 5 giây, đều trên máy này:

- **Render bản đã duyệt.** Khi dashboard duyệt một revision, worker nhận dòng
  `localization.final_renders`, dựng đúng revision đó từ asset bất biến trong MinIO ra thư mục
  tạm `data/review-render/` (không phải `data/review/`), chạy `hyperframes render -q delivery`
  rồi upload `output/review/<aweme>/g<gen>/<revision>.mp4` và `.srt`. Studio đang mở, bridge
  đang ghi hay chỉnh sửa chưa lưu đều không ảnh hưởng bản render. Mỗi render có timeout cứng
  30 phút, lease 45 phút (gia hạn mỗi 5 phút) và tối đa 3 lần thử; thư mục tạm luôn bị xóa.
  Dưới `REVIEW_WORKER_MIN_FREE_GB` (mặc định 5) đĩa trống thì worker không nhận việc và ghi lý
  do vào heartbeat. `FINAL_RENDER_WORKERS` (mặc định 2) là số Chrome của một render.
- **Quét review.** Mỗi 60 giây ghi `localization.review_status`: số cue, số mismatch và
  project có chỉnh sửa chưa lưu hay không. Chỉ đọc: không materialize, không ghi marker (host
  và container bridge không có khóa file chung đáng tin cậy, nên chỉ bridge ghi marker).
- **Mở Studio theo yêu cầu** của dashboard (`studio_open_requests`) qua
  `scripts/review-studio-request.mjs`: sao lưu rồi dừng Studio cũ nếu có, mở project mới. Studio
  được khởi chạy tách khỏi worker nên vẫn mở khi worker khởi động lại.
- **Heartbeat** vào `localization.host_workers` (tên `review-worker`): đĩa trống, tình trạng
  VoiceStudio và render đang chạy. Dừng worker thì dashboard báo `down` sau 2 phút.

`pm2 stop reup-review-worker` (hoặc reload) khiến worker đợi render đang chạy tối đa 60 giây,
rồi trả dòng về `queued`, kill cả process group của renderer. `kill_timeout` của app là 90 giây
vì pm2 mặc định chỉ đợi khoảng 1,6 giây, và `treekill: false` để pm2 không kill cả cây tiến
trình ngay (renderer chết trước khi worker kịp trả dòng về hàng đợi). Worker dừng thì dòng
`rendering` giữ nguyên tới khi lease hết hạn (45 phút) hoặc worker chạy lại; trong lúc đó
dashboard chưa cho duyệt lại hay trả về. `reup-review-auto-open` chỉ mở project mới khi không có
Studio nào đang mở, nên không đóng project vừa mở từ dashboard.

Export tay trong Studio ghi `renders/*.meta.json`; thư mục `renders/` không còn tính vào
dấu vân tay của project nên export không làm project bị coi là có chỉnh sửa chưa lưu. Marker
cũ (tính theo cách trước, giữ nguyên revision của nó) hoặc project khớp từng byte với HEAD được
bridge ghi lại marker mới một lần, trong `materialize` và khi lưu; thư mục có chỉnh sửa thật
không bị động tới. Revision cũ có `renders/*.meta.json` trong `studio_files` vẫn materialize
được: các file đó bị bỏ qua. Sau khi đổi code `review_bridge`, rebuild
image: `docker compose --profile review build review-bridge` rồi `up -d --no-deps review-bridge`.

Kiểm chứng ban đầu (HyperFrames 0.8.77, project `7689288513854595771/g3`, 189 s): bản render
của worker khớp bản Studio export về độ phân giải 1024x576, 30 fps, thời lượng 189,67 s, âm
thanh AAC, và khung hình có phụ đề (font, viền, hộp che) giống nhau (SSIM khoảng 0,98). Khác
biệt duy nhất là bitrate vì worker dùng `-q delivery` (khoảng 98 MB so với 66 MB) và thời gian
render khoảng 2 phút 10 giây.

Cờ Chrome: HyperFrames khởi chạy `chrome-headless-shell` với `--no-sandbox` và
`--disable-setuid-sandbox` theo mặc định, CLI 0.8.77 không có tùy chọn bật sandbox. Chrome
chỉ nạp composition do pipeline tự dựng (CSP cố định, văn bản đã escape, asset từ revision
bất biến) và chạy với môi trường tối thiểu (`PATH`, `HOME`, `LANG`, `TMPDIR`, không có
credential DB hay S3). Đây là rủi ro đã biết, cần người dùng chấp nhận.

## Kiểm tra

```sh
node --test scripts/tests/review-studio.test.mjs
cd services/localization
uv run pytest -q tests/review_bridge tests/activities/test_revoice_caption.py tests/workflows/test_revoice_caption.py
uv run ruff check src tests
```

Browser fixture dùng media tổng hợp, Moto và Temporal local thật, không provider
hay bucket production. Kiểm tra port 3310 trống trước khi chạy, giữ process ở
foreground và dừng bằng Ctrl+C sau test:

```sh
cd services/localization
uv run python -m tests.support.review_companion
# Terminal khác, từ repo root, Node 22+ có global WebSocket:
REVIEW_BROWSER_FIXTURE=1 node scripts/tests/review-companion-browser.mjs
```

Gate backend/provider/Studio thật chạy riêng. Cần bridge, worker và Studio đã đạt
boundary gate; một generation thử nghiệm riêng được phép sửa; và các biến
`REVIEW_LIVE_DEDICATED_FIXTURE=1`, `REVIEW_LIVE_AWEME`, `REVIEW_LIVE_GENERATION`,
`REVIEW_LIVE_CUE`, `REVIEW_LIVE_TEXT` (text vừa slot). Test cố ý lưu/revoice fixture,
không tự hoàn tác hoặc trỏ vào bản review đang làm:

```sh
cd services/localization
uv run pytest -q tests/integration/test_review_roundtrip.py -m live
```

Thiếu prerequisite thì test skip; skip và suite dùng provider giả không phải bằng
chứng live. Migration/canary/activation và rollback theo [runbook triển khai](hyperframes-review.md);
Mac hiện tại được bật theo yêu cầu rõ ràng của người dùng; các gate native Studio
và live provider còn lại vẫn phải được nghiệm thu riêng.

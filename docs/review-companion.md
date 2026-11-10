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

## Lưu và phục hồi

Native preview dùng runtime tại origin Studio cố định trong CSP. Element có
editor ID ổn định; preview, waveform, thumbnail cache và stamp metadata không
được tính là source edits. HTML/CSS/JSON nguồn và overrides vẫn được theo dõi.

Box mặc định trắng ở alpha 0.25, hiện suốt video, rộng 60% và căn giữa (20% mỗi
bên). Font giữ 20px. Generator đo bằng font đi kèm, xuống dòng và nới box khi
câu dài; không tự thu nhỏ chữ. Box chỉ nới trong thời gian câu dài, rồi về 60%; các
khoảng không có sub vẫn có box, không fade hoặc biến mất.
Line-height là 28px (tăng 3px giữa các dòng so với trước).
Viền chữ là `-webkit-text-stroke: 4px #000` trong `.caption` ở Code → `index.html`.
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
rollout vẫn phải chờ native Studio và live provider gates.

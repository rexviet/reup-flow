# Chạy bước render trên máy Windows (LAN, native)

Mục tiêu: sau khi bấm **Duyệt** trên dashboard, bước render bản cuối (HyperFrames
`render -q delivery`) chạy trên PC Windows có CPU khỏe hơn, cùng mạng LAN, chạy **native
Windows** (không WSL, không Docker). Mac giữ dashboard, Docker stack, Studio, speech và Demucs;
Windows chỉ nhận dòng `localization.final_renders`, render, upload MP4/SRT.

Tài liệu dựa trên khảo sát mã nguồn, chưa chạy thử trên Windows. Chỗ nào chưa kiểm chứng thì ghi
rõ. Các bản vá ở mục 3 (G1 đến G8) **đã có trong repo**, kiểm bằng test trên Mac; nhánh Windows
chỉ được test bằng mock, lần chạy thật đầu tiên là mục 6.

**Quyết định đã chốt:**

- Chạy native Windows (không WSL).
- Không làm smoke test HyperFrames riêng trước khi vá; việc kiểm chứng gộp vào lần render thật đầu
  tiên (mục 6). Nếu HyperFrames 0.8.77 lỗi trên Windows thì xử lý lúc đó (mục 5.5).
- Mạng LAN được coi là tin cậy: không lọc theo IP nguồn, không dùng Tailscale. Vẫn bind vào một IP
  cụ thể của Mac và dùng mật khẩu mạnh.
- Giữ **một render mỗi lần** như hiện nay; không thêm render song song trong đợt này.
- Windows có GPU: thêm cờ tùy chọn để dùng GPU mã hóa (mục 5.7), mặc định tắt để Mac không đổi.

## 1. Kết luận

| Câu hỏi | Trả lời |
|---|---|
| Bước render có gắn chặt với Mac không? | Phần render thuần (`final_render.py` + `hyperframes render`) **không gắn Mac**: chỉ cần Postgres, MinIO, Node, Chrome headless, ffmpeg. Phần **gắn Mac** là cách khởi chạy (`review-worker.mjs` đọc `docker compose config`) và hai việc khác nằm chung tiến trình worker (mở Studio, quét review). |
| Chạy native Windows được không? | **Được sau khi vá**: có đúng hai chỗ POSIX-only trên đường chạy của worker render (`import fcntl` ở `repository.py`; `os.killpg`/`SIGKILL`/`start_new_session` ở `final_render.py`), cộng với môi trường con thiếu biến Windows. HyperFrames có hỗ trợ Windows theo tài liệu công khai (Node 22+, ffmpeg); bản 0.8.77 ghim ở đây chưa được thử, sẽ kiểm bằng lần render thật đầu tiên (mục 5.5, 6). |
| Hệ thống sẵn sàng nhận kết nối từ LAN chưa? | **Chưa.** Mọi cổng host đều bind `127.0.0.1`. Worker render chỉ cần **Postgres (5433)** và **MinIO S3 (9000)** từ Mac; không cần Temporal, Redis, nest, Langfuse. |
| Hai worker chạy cùng lúc có dẫm chân không? | Claim render an toàn (`FOR UPDATE SKIP LOCKED`, claim token, lease). Nhưng worker hiện gói 4 việc vào một tiến trình; chạy nguyên bản trên Windows nó sẽ tranh mở Studio, ghi đè `review_status` và heartbeat của Mac (G1). |

## 2. Kết quả khảo sát

### 2.1 Bước render cần gì

Dashboard duyệt (`dashboard.actions.ts`) ghi `review_decisions` rồi chèn
`localization.final_renders` trạng thái `queued`. Worker (`review_worker/main.py`, vòng 5 giây)
gọi `final_render.claim` rồi `render_claimed`:

1. Đọc HEAD và manifest của revision từ MinIO (`review/<aweme>/g<gen>/...`), xuất đúng revision ra
   thư mục tạm `data/review-render/final-render-*/project` (không đụng `data/review/` của Studio).
2. Chạy `node <HYPERFRAMES_BIN> render <folder> -o final.mp4 -q delivery --workers N` trong process
   group riêng, môi trường tối thiểu (không có credential).
3. Upload `output/review/<aweme>/g<gen>/<revision>.mp4`, `.srt` và poster `.jpg` (ffmpeg) lên
   MinIO, ghi `state='rendered'`.

| Cần | Chi tiết | Nguồn |
|---|---|---|
| Postgres | `LOC_DATABASE_URL` (role `localization`, DB `reup`) | `review_worker/config.py` |
| MinIO S3 | `S3_ENDPOINT`, `S3_BUCKET`, `LOC_S3_ACCESS_KEY_ID/SECRET`; policy `localization` đọc `review/*`, **ghi** `output/*` | `infra/minio/init.sh`, `storage/object_store.py` |
| Python 3.12 + gói `localization` | `uv sync --frozen --no-dev` (kéo cả torch CPU dù render không dùng; lockfile có wheel Windows của torch 2.5.1 hay không chưa kiểm chứng) | `pyproject.toml` |
| Node ≥ 22.13 + `hyperframes@0.8.77` | `pnpm install --frozen-lockfile` trong `services/localization/spikes/hyperframes-review`; `HYPERFRAMES_BIN` trỏ tới `node_modules\hyperframes\bin\hyperframes.mjs` | `scripts/review-studio.mjs` (`cli()`) |
| Chrome headless | HyperFrames tự tải `chrome-headless-shell` vào `%USERPROFILE%\.cache\hyperframes\chrome` lần đầu | `docs/review-companion.md` |
| ffmpeg trên PATH | poster (`posters.py`) và renderer | `posters.py` |
| Font | không cần cài: font nằm trong manifest của revision | `repository.export_revision` |

Không cần: Temporal, Redis, nest, parser, Langfuse, VoiceStudio, Docker, `data/review` của Mac.

### 2.2 Phần gắn với Mac hoặc POSIX

| Chỗ gắn | Hậu quả trên Windows | Xử lý |
|---|---|---|
| `scripts/review-worker.mjs` + `lib/host-voice-env.mjs` lấy env từ `docker compose config` | Không có compose của Mac | Script PowerShell nạp file env (G2) |
| `studio_requests.py` chạy `review-studio-request.mjs` (Studio local, cổng 3002, `lsof`, `docker compose exec`) | Nhận yêu cầu "Mở Studio" rồi báo lỗi hoặc mở sai máy | Tắt bằng vai trò `render` (G1) |
| `review_scan.py` đọc `data/review` cục bộ | Thư mục trống nên ghi `dirty=false` sai đè kết quả của Mac | Tắt bằng vai trò `render` (G1) |
| Heartbeat tên cố định `review-worker`; dashboard đọc đúng tên này | Hai worker ghi đè nhau | Tên riêng (G1) |
| `repository.py` dòng 4: `import fcntl` (chỉ dùng trong `lock()`, đường render không gọi) | **Import lỗi ngay khi khởi động** | Tầng `platform`: `FileLock` (G5) |
| `final_render.py`: `os.killpg`, `signal.SIGKILL`, `start_new_session=True` | `AttributeError`/không kill được Chrome | Tầng `platform`: `ProcessControl` (G5) |
| `render_env()` chỉ truyền `PATH, TMPDIR, HOME, LANG` | Node/Chrome trên Windows cần `SystemRoot`, `TEMP`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`…; thiếu thì lỗi mạng/khởi động | `ProcessControl.child_env` (G5) |
| SIGTERM làm tín hiệu dừng | Windows không gửi SIGTERM cho tiến trình thường | Thêm `SIGBREAK`/Ctrl+C (G6) |
| Chrome macOS (Studio preview) khác Chrome Windows (render) | Khác biệt khử răng cưa chữ rất nhỏ; layout phụ đề đã đo sẵn bằng font gói kèm | Kiểm bằng một video thật (mục 6) |

### 2.3 Mức sẵn sàng của LAN

| Dịch vụ | Cổng host | Bind hiện tại | Windows cần? |
|---|---|---|---|
| Postgres | 5433 | `127.0.0.1` | **Có** |
| MinIO S3 API | 9000 | `127.0.0.1` (do override) | **Có** |
| MinIO console, Temporal, nest, web, Redis, Langfuse, review-bridge | 9001, 7233, 3300, 5173, 6379, 3100, 3310 | `127.0.0.1` | Không, giữ nguyên |

Thiết kế cố ý "loopback only" (`compose-constraints.spec.ts` ép điều này), nên việc mở LAN phải là
một file compose riêng, không sửa file mặc định.

Rủi ro cần xử lý **trước** khi mở 5433 ra LAN:

- `POSTGRES_PASSWORD` mặc định là `reup` (`${POSTGRES_PASSWORD:-reup}`) cho superuser `reup`. Phải
  chắc `.env` đặt mật khẩu mạnh. (Tôi không đọc `.env` vì chứa secret; hãy tự kiểm.)
- Worker chỉ dùng role `localization`; không đưa thông tin superuser `reup` sang Windows.
- Postgres và S3 qua `http` thuần: dữ liệu và mật khẩu đi không mã hóa. Đã chốt chấp nhận trên LAN
  tin cậy.
- Bind vào **một IP cụ thể** của Mac, không dùng `0.0.0.0`; đặt DHCP reservation cho Mac (nếu IP đổi
  thì Docker không start được cổng đó).

## 3. Bản vá code

Đã làm theo đúng trình tự ở mục 3.1. Test: `tests/review_worker/test_posix_process_control.py`
(khóa hành vi POSIX), `tests/review_worker/test_platform.py` (bản Windows qua mock, factory, vai
trò, GPU), hai test vai trò trong `test_scan_studio_worker.py`, và
`scripts/tests/review-studio-request.test.mjs` (truyền biến vai trò từ pm2).

| Bản vá | Nằm ở |
|---|---|
| G1, G6, G8 | `review_worker/config.py` (`REVIEW_WORKER_ROLES`, `REVIEW_WORKER_NAME`, `FINAL_RENDER_GPU`), `main.py` (`stop_signals`), `heartbeat.py`, `final_render.render_command` |
| G2 | `scripts/render-worker.ps1`: nạp `.render-worker.env`, từ chối khóa bí mật của Mac, tự đặt `render`/`render-worker-win` nếu thiếu |
| G3 | `ecosystem.config.cjs` (dòng `env` đang tắt) và `scripts/lib/review-worker-env.mjs` (chuyển `REVIEW_WORKER_ROLES`, `REVIEW_WORKER_NAME`, `FINAL_RENDER_GPU` từ pm2 vào worker) |
| G4 | `docker-compose.lan.yml` (từ chối chạy khi `LAN_BIND_IP` rỗng) |
| G5 | `localization/platform/` (`base`, `posix`, `windows`, `factory`) |
| G7 | `docs/review-companion.md`, `docs/admin-dashboard.md` |

Bảng dưới là phạm vi ban đầu của từng bản vá.

| # | Việc | File |
|---|---|---|
| G1 | **Vai trò worker**: `REVIEW_WORKER_ROLES` (`render` hoặc `all`; mặc định `all` để Mac không đổi). `render` bỏ qua Studio requests, review scan, poster backfill. Tên heartbeat từ `REVIEW_WORKER_NAME` (mặc định `review-worker`; Windows dùng `render-worker-win`). | `review_worker/main.py`, `heartbeat.py` |
| G2 | **Script khởi chạy PowerShell** `scripts/render-worker.ps1`: nạp `.render-worker.env`, chạy `python -m localization.review_worker.main`, thay cho `review-worker.mjs`. | mới |
| G3 | **Tắt render ở Mac**: vai trò `studio` (không claim render) cho `reup-review-worker` trên Mac. | `main.py`, `ecosystem.config.cjs`/`review-worker-env.mjs` |
| G4 | **`docker-compose.lan.yml`** thêm `${LAN_BIND_IP}:5433:5432` cho `postgres` và `${LAN_BIND_IP}:9000:9000` cho `minio`. | mới |
| G5 | **Tầng `localization/platform/`** gồm hai interface hẹp `FileLock` và `ProcessControl`, hai bản cài đặt (`posix`, `windows`) và một factory chọn theo env. Chi tiết và ràng buộc không-regression ở mục 3.1. | mới: `platform/`; sửa `repository.py`, `final_render.py` |
| G6 | **Dừng êm trên Windows**: bắt thêm `SIGBREAK` (và `SIGINT`) để chạy `shutdown()` (đợi render tối đa 60 giây, trả dòng về `queued`). Trên POSIX giữ nguyên `SIGTERM`/`SIGINT`. | `review_worker/main.py` |
| G8 | **Cờ GPU tùy chọn** `FINAL_RENDER_GPU` (mặc định tắt) cho `render_command`; chi tiết mục 5.7. Làm sau cùng, khi render CPU trên Windows đã chạy. | `review_worker/config.py`, `final_render.py` |
| G7 | Cập nhật `docs/review-companion.md`, `docs/admin-dashboard.md` khi các mục trên vào (tài liệu cũ ghi timeout 30 phút/lease 45 phút; code hiện là 60/75 trong `config.py`). | docs |

### 3.1 Thiết kế tầng platform (không làm regression luồng Mac)

**Phạm vi trừu tượng hóa: chỉ phần khác nhau theo OS.** Trong `repository.py` đó là khóa file
`fcntl.flock` ở `lock()`. Trong `final_render.py` đó là quản lý process group (`start_new_session`,
`killpg`, `group_alive`, `kill_group`) và `render_env()`. Claim SQL, lease, retry, upload, export
revision đều là logic thuần, **không** tách thành lớp con theo OS (nếu tách sẽ phải nhân bản logic
đó cho từng OS).

```python
# localization/platform/base.py
class FileLock(Protocol):
    def hold(self, path: Path) -> ContextManager[None]: ...

class ProcessControl(Protocol):
    def spawn(self, cmd, *, cwd, env, stdout) -> subprocess.Popen: ...  # group/job riêng
    def terminate_tree(self, proc, grace_s: float) -> None: ...         # POSIX: SIGTERM→SIGKILL; Windows: taskkill /T /F
    def child_env(self, tmp: Path) -> dict[str, str]: ...               # biến môi trường của tiến trình con
```

| Bản cài đặt | Dùng cho | Ghi chú |
|---|---|---|
| `PosixFileLock`, `PosixProcessControl` | **Mac và Linux dùng chung**, không có lớp riêng cho Mac | Mã hiện tại được **chuyển nguyên văn**, không viết lại (xem dưới). Khác biệt EPERM/ESRCH của macOS đã nằm sẵn trong `group_alive`. |
| `WindowsFileLock`, `WindowsProcessControl` | Windows | `WindowsFileLock` báo `NotSupported` (chỉ bridge trên Mac gọi `lock()`). `WindowsProcessControl`: `CREATE_NEW_PROCESS_GROUP`, dừng bằng `taskkill /PID <pid> /T /F`. Hạn chế đã biết: `taskkill /T` đi theo cây cha-con nên không dọn được Chrome mồ côi nếu tiến trình cha đã thoát trước; nếu gặp, chuyển sang Job Object (`ctypes`). |

**Factory:** `get_platform()` đọc `REVIEW_PLATFORM` với ba giá trị `auto` (mặc định), `posix`,
`windows`. `auto` quyết định bằng `os.name`. Giá trị lạ làm worker **dừng ngay khi khởi động** (không
im lặng rơi về mặc định). Ép `windows` trên máy POSIX (hay ngược lại) bị từ chối trừ khi có
`REVIEW_PLATFORM_FORCE=1`; biến này chỉ để test, còn test đơn vị nên tiêm bản cài đặt qua tham số
thay vì qua env.

**Các ràng buộc để Mac không đổi hành vi:**

1. **Mặc định giữ nguyên.** Không đặt biến nào thì Mac nhận `auto` → `posix`, vai trò `all`, tên
   heartbeat `review-worker`. `ecosystem.config.cjs` và `review-worker-env.mjs` của Mac không cần thêm
   biến nào cho phần này.
2. **Chuyển nguyên văn, không viết lại.** Nội dung `group_alive`, `kill_group`, cách `Popen`
   (`start_new_session=True`, `stdin=DEVNULL`, gộp stderr vào stdout) và `render_env` được đưa vào
   `PosixProcessControl` không đổi một ký tự logic: cùng tín hiệu, cùng vòng chờ 100 × 0,1 giây, cùng
   `SIGKILL` cuối, cùng `wait(5)`, cùng tập khóa env `PATH, TMPDIR, HOME, LANG`.
3. **Giữ nguyên tên công khai.** `final_render.run_cli`, `group_alive`, `kill_group`, `render_env` vẫn
   tồn tại với cùng chữ ký và ủy quyền sang `platform`. Test hiện có import `run_cli` trực tiếp, và
   `render_claimed(runner=run_cli)` vẫn là điểm tiêm; không test nào phải sửa import.
4. **`import fcntl` dời vào trong `PosixFileLock.hold`.** Trên Mac, `fcntl` vẫn được nạp trước khi
   khóa lần đầu; bridge trong container và luồng materialize không đổi. Trên Windows module không bao
   giờ nạp `fcntl`, nên import `repository` thành công.
5. **`Repository` nhận `FileLock` qua constructor, mặc định lấy từ factory.** Mọi chỗ đang gọi
   `Repository(store, root)` (worker, bridge, test) không phải sửa.
6. **Không đụng phạm vi ngoài.** `activities/render.py` và `providers/demucs.py` (chạy trong container
   Linux) cũng dùng `killpg` nhưng không thuộc luồng này và không được sửa trong đợt này.

**Trình tự thực hiện, mỗi bước một commit, chỉ bước 2 có rủi ro với Mac:**

| Bước | Nội dung | Cổng kiểm tra |
|---|---|---|
| 1 | Thêm test đặc trưng cho hành vi POSIX hiện tại, **không đổi code chạy**: tập khóa env của `render_env`, cờ `start_new_session`, thứ tự SIGTERM → SIGKILL, dọn con mồ côi, khóa `flock` thực sự chặn tiến trình thứ hai. Đã có sẵn `test_timeout_kills_the_whole_process_group` và `test_children_left_behind_by_a_finished_renderer_are_killed` (`tests/review_worker/test_final_render.py`). | Toàn bộ xanh trên commit gốc. |
| 2 | Tạo `platform/` chỉ với bản POSIX và nối vào `repository.py`, `final_render.py` (refactor thuần). | Các test ở bước 1 và toàn bộ `tests/review_worker`, `tests/review_bridge` vẫn xanh **không sửa test**; `ruff check src tests`; hai lần render thật trên Mac (trước và sau) cho cùng `state`, tập khóa S3 và `error` rỗng. |
| 3 | Thêm bản Windows, factory đầy đủ, vai trò worker (G1), `SIGBREAK` (G6). Test nhánh Windows bằng mock `subprocess`/`taskkill` và `os.name`; test factory (`auto`, giá trị lạ, ép nền tảng, `FORCE`). | Test mới xanh trên Mac; bộ test bước 1 vẫn xanh. |
| 4 | Script PowerShell, `docker-compose.lan.yml`, tài liệu (G2, G3, G4, G7). | Mục 6; riêng G3 chỉ bật sau khi Windows render thành công một video. |
| 5 | Cờ GPU (G8), mặc định tắt. | Test khóa nguyên lệnh render khi cờ tắt (Mac không đổi); đo và so chất lượng theo mục 5.7. |

Thứ tự này giữ đúng nguyên tắc của dự án: mọi thay đổi hành vi (vai trò, tắt render ở Mac) đến
**sau** khi phần refactor đã chứng minh không đổi gì, và G3 (tắt render ở Mac) là bước cuối cùng, nên
Mac vẫn tự render được trong lúc Windows chưa chứng minh được.

**Cách quay lại nếu bước 2 làm hỏng Mac:** revert đúng commit đó; bước 1 chỉ là test nên giữ nguyên.

## 4. Cài đặt phía Mac (một lần)

1. Đặt DHCP reservation cho Mac trên router; ghi `MAC_IP` (ví dụ `192.168.1.10`).
2. Kiểm `.env`: `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`, `LOCALIZATION_DB_PASSWORD`,
   `LOC_S3_ACCESS_KEY_ID/SECRET` đều mạnh, không phải mặc định.
3. Thêm `docker-compose.lan.yml` (G4):

   ```yaml
   services:
     postgres:
       ports:
         - "${LAN_BIND_IP}:5433:5432"
     minio:
       ports:
         - "${LAN_BIND_IP}:9000:9000"
   ```

   Trong `.env`: `LAN_BIND_IP=<MAC_IP>` và
   `COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.lan.yml`.
4. `docker compose up -d --no-deps postgres minio`.
5. Không lọc theo IP nguồn (LAN tin cậy). Bảo vệ còn lại: bind đúng một IP, mật khẩu mạnh, và các
   cổng khác vẫn chỉ ở `127.0.0.1`.
6. **Chỉ sau khi Windows đã render thành công một video** (mục 6): bỏ dấu `//` trước dòng
   `env: { REVIEW_WORKER_ROLES: 'studio' }` của `reup-review-worker` trong `ecosystem.config.cjs`
   (G3), rồi `pm2 delete reup-review-worker && pm2 start ecosystem.config.cjs --only
   reup-review-worker` (`pm2 reload` không đọc lại `env` của file cấu hình).
7. Kiểm: `lsof -nP -iTCP:5433 -iTCP:9000 -sTCP:LISTEN` thấy `MAC_IP:5433`, `MAC_IP:9000` cùng các
   dòng `127.0.0.1`; không được có `*:5433`.

## 5. Cài đặt phía Windows (native)

Chỉ có kết nối **đi ra** từ Windows tới Mac; không cần mở cổng vào Windows và không cần rule
Windows Firewall (nếu firewall outbound của bạn mặc định chặn, cho phép `python.exe` và `node.exe`).

### 5.1 Công cụ

PowerShell thường (không cần quản trị cho phần lớn lệnh; winget có thể hỏi UAC):

```powershell
winget install --id Git.Git -e
winget install --id OpenJS.NodeJS.LTS -e        # phải >= 22.13; kiểm: node -v
winget install --id Gyan.FFmpeg -e
winget install --id astral-sh.uv -e
corepack enable
corepack prepare pnpm@11.23.0 --activate
# Mở lại PowerShell để PATH cập nhật, rồi kiểm:
node -v; ffmpeg -version; uv --version; pnpm -v
```

Nếu `node -v` thấp hơn 22.13, cài bản mới hơn bằng `winget install OpenJS.NodeJS` hoặc `fnm`.
Bật đường dẫn dài (PowerShell quản trị, rồi khởi động lại):
`New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force`.

Đặt repo ở đường dẫn ngắn trên ổ nhanh (SSD/NVMe), ví dụ `C:\reup-flow`, không đặt trong OneDrive.

### 5.2 Mã nguồn và phụ thuộc

```powershell
git clone <repo-url> C:\reup-flow; cd C:\reup-flow
git checkout <cùng nhánh/commit với Mac>      # hai bên phải cùng phiên bản code
cd services\localization; uv sync --frozen --no-dev
cd spikes\hyperframes-review; pnpm install --frozen-lockfile
```

Không cần `git submodule update` (submodule chỉ phục vụ parser). Nếu `uv sync` không tìm được wheel
Windows cho `torch==2.5.1` từ index CPU, đó là khoảng trống cần xử lý (đưa `torch` và
`silero-vad` ra một nhóm phụ thuộc tùy chọn) trước khi đi tiếp.

### 5.3 File env

Tạo `C:\reup-flow\.render-worker.env` (`.gitignore` có dòng riêng cho tên này). Lấy giá trị từ
`.env` của Mac, **chỉ** các khóa sau; dùng dấu `/` hoặc `\\` đều được, nhưng phải là đường dẫn
tuyệt đối:

```ini
LOC_DATABASE_URL=postgresql://localization:<LOCALIZATION_DB_PASSWORD>@<MAC_IP>:5433/reup
S3_ENDPOINT=http://<MAC_IP>:9000
S3_REGION=us-east-1
S3_BUCKET=reup-media
S3_FORCE_PATH_STYLE=true
LOC_S3_ACCESS_KEY_ID=...
LOC_S3_SECRET_ACCESS_KEY=...

REVIEW_WORKER_ROLES=render
REVIEW_WORKER_NAME=render-worker-win
REVIEW_WORKER_DATA_ROOT=C:/reup-flow/data/review
REVIEW_WORKER_NODE=C:/Program Files/nodejs/node.exe
HYPERFRAMES_BIN=C:/reup-flow/services/localization/spikes/hyperframes-review/node_modules/hyperframes/bin/hyperframes.mjs
FINAL_RENDER_WORKERS=8
REVIEW_WORKER_MIN_FREE_GB=10
```

Giới hạn quyền file: `icacls C:\reup-flow\.render-worker.env /inheritance:r /grant:r "$env:USERNAME:R"`.
Không đưa vào: `OPENROUTER_API_KEY`, `API_KEY`, `POSTGRES_PASSWORD`, `MINIO_ROOT_*`, `LANGFUSE_*`,
`REVIEW_S3_*`. `render-worker.ps1` từ chối khởi động nếu file có một trong các khóa này, hoặc
thiếu `LOC_DATABASE_URL`, `S3_ENDPOINT`, `LOC_S3_ACCESS_KEY_ID`, `LOC_S3_SECRET_ACCESS_KEY`,
`REVIEW_WORKER_DATA_ROOT`, `HYPERFRAMES_BIN`. Mỗi dòng là `KHÓA=giá trị`; dòng trống và dòng
bắt đầu bằng `#` được bỏ qua.

`FINAL_RENDER_WORKERS` là số Chrome song song của **một** render (worker render một video mỗi lần).
Bắt đầu 8, đo, rồi tăng dần tới khoảng số nhân vật lý trừ 2; mỗi Chrome tốn RAM đáng kể nên theo dõi
bộ nhớ.

### 5.4 Kiểm tra kết nối (trước khi chạy worker)

```powershell
cd C:\reup-flow\services\localization
Get-Content ..\..\.render-worker.env | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), 'Process') }
}
Invoke-WebRequest -UseBasicParsing "http://<MAC_IP>:9000/minio/health/ready"   # 200
Test-NetConnection <MAC_IP> -Port 5433                                          # TcpTestSucceeded: True
uv run python -c "from localization.config import load_settings; from localization.storage.object_store import ObjectStore; s=ObjectStore.from_settings(load_settings()); print('store ok', s.exists('review/__probe__'))"
uv run python -c "import psycopg,os; c=psycopg.connect(os.environ['LOC_DATABASE_URL'],connect_timeout=5); print(c.execute('select count(*) from localization.final_renders').fetchone())"
& $env:REVIEW_WORKER_NODE $env:HYPERFRAMES_BIN --version                        # 0.8.77
```

`store ok False` là bình thường; lỗi 403 hoặc timeout nghĩa là sai policy hoặc mạng.

### 5.5 HyperFrames trên Windows (kiểm chứng gộp vào lần render đầu)

Không có bước smoke test riêng. Lần render thật đầu tiên (mục 6) là phép thử: HyperFrames sẽ tải
`chrome-headless-shell` về `%USERPROFILE%\.cache\hyperframes` (cần Internet) rồi render. Có thể chạy
trước `node node_modules\hyperframes\bin\hyperframes.mjs doctor` trong
`services\localization\spikes\hyperframes-review` nếu bản 0.8.77 có lệnh này (chưa xác nhận).

Nếu render lỗi vì lý do đặc thù Windows của chính HyperFrames (đường dẫn, spawn không `shell:true`,
ffmpeg), có hai lối ra: nâng ghim phiên bản (phải xác minh lại runtime hash như
`docs/review-companion.md` yêu cầu, trên cả hai máy), hoặc quay lại phương án WSL2. Vì dòng
`final_renders` tự trả về `queued` hoặc `failed` kèm `error`, một lần thử lỗi không làm hỏng dữ liệu.

### 5.6 Chạy đúng phần render

```powershell
cd C:\reup-flow
powershell -ExecutionPolicy Bypass -File scripts\render-worker.ps1        # G2
```

Log đầu phải là `review worker started; data root ...; roles render; name render-worker-win;
platform windows`. Dừng bằng Ctrl+C (hoặc Ctrl+Break): worker đợi render đang chạy tối đa 60 giây
rồi trả dòng về `queued`. Renderer chạy trong process group riêng nên Ctrl+C không chạm tới nó;
worker tự dừng nó bằng `taskkill /T /F`.

Chạy nền và tự bật lại sau khởi động: **Task Scheduler** (không cần phần mềm ngoài):

- Trigger: *At startup* (hoặc *At log on*). Run whether user is logged on or not nếu muốn chạy khi
  chưa đăng nhập (cần mật khẩu tài khoản).
- Action: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\reup-flow\scripts\render-worker.ps1`,
  Start in `C:\reup-flow`.
- Settings: *If the task fails, restart every 1 minute, up to 10 times*; bỏ giới hạn thời gian chạy
  ("Stop the task if it runs longer than" tắt).
- Tắt ngủ: `powercfg /change standby-timeout-ac 0` và `powercfg /change hibernate-timeout-ac 0`.

Lưu ý: Task Scheduler dừng tác vụ bằng kết thúc cưỡng bức, nên render đang chạy **không** được trả về
hàng đợi ngay; dòng giữ `rendering` đến khi lease (75 phút) hết hạn rồi được nhận lại (tối đa 3 lần
thử). Nếu muốn dừng êm như Mac, chạy worker bằng NSSM (`nssm install`, đặt *Shutdown* gửi Ctrl+C với
timeout 90 giây). Với tốc độ Windows mới, đây là chi phí chấp nhận được.

Tăng tốc tùy chọn: thêm loại trừ Windows Defender cho `C:\reup-flow\data` và
`%USERPROFILE%\.cache\hyperframes` (`Add-MpPreference -ExclusionPath ...`, cần quản trị), vì render
ghi rất nhiều file tạm. Đây là đánh đổi bảo mật nhỏ; chỉ làm nếu đo thấy chậm.

### 5.7 Dùng GPU (tùy chọn, bật sau khi render bằng CPU đã chạy)

Windows có GPU, nhưng nên hiểu GPU giúp được phần nào: HyperFrames chụp từng khung bằng Chrome
headless rồi ffmpeg mã hóa thành MP4. GPU chỉ có thể tăng tốc **bước mã hóa** (và có thể cả
compositing của Chrome nếu có), còn chụp khung phần lớn chạy trên CPU. Vì vậy hãy đo bản CPU trước;
GPU cho lợi ích thật chỉ khi mã hóa chiếm phần đáng kể thời gian.

Tình trạng hiểu biết: tài liệu công khai của HyperFrames mô tả cờ `--gpu` ("dùng GPU hệ thống để mã
hóa tăng tốc phần cứng") nhưng không nói bộ mã hóa nào được chọn hay có chọn được không, và tôi chưa
xác nhận cờ này tồn tại ở bản ghim 0.8.77. Cách làm:

1. Cài driver GPU mới nhất. Với NVIDIA, kiểm ffmpeg có bộ mã hóa phần cứng:
   `ffmpeg -hide_banner -encoders | findstr /i "nvenc amf qsv"` (bản `Gyan.FFmpeg` full thường có;
   AMD dùng `h264_amf`, Intel dùng `h264_qsv`).
2. Xem cờ thật của bản ghim: `node ...\hyperframes.mjs render --help`.
3. Đặt `FINAL_RENDER_GPU=true` trong `.render-worker.env` (đã có trong code, mặc định `false`):
   `render_command` thêm `--gpu` vào cuối lệnh. Nếu bước 2 cho thấy cờ thật có tên khác, sửa ở
   `final_render.render_command`. Mac không đặt biến này nên lệnh render của Mac không đổi; test
   `test_render_command_is_unchanged_without_the_gpu_flag` khóa nguyên lệnh.
4. So sánh kết quả bản GPU với bản CPU `-q delivery`: kích thước (bản CPU khoảng 98 MB cho video 189
   giây), chất lượng (SSIM so với bản Mac), thời gian. Bộ mã hóa phần cứng thường cho chất lượng thấp
   hơn x264 ở cùng bitrate; chỉ bật mặc định nếu chất lượng chấp nhận được.

### 5.8 SSH từ Mac vào Windows (tùy chọn)

Để điều khiển máy Windows từ Mac. Dùng OpenSSH Server có sẵn của Windows 10/11. Chưa thử trên máy
Windows của dự án.

**1. Cài và bật sshd** (PowerShell **quản trị**):

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic

# Rule firewall (bản cài thường tự tạo); nếu lệnh đầu báo không có thì chạy lệnh thứ hai
Get-NetFirewallRule -Name OpenSSH-Server-In-TCP | Select-Object Enabled, Profile
New-NetFirewallRule -Name OpenSSH-Server-In-TCP -DisplayName 'OpenSSH Server (sshd)' `
  -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22

# PowerShell làm shell mặc định khi SSH vào (mặc định là cmd)
New-ItemProperty -Path HKLM:\SOFTWARE\OpenSSH -Name DefaultShell `
  -Value C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -PropertyType String -Force

# Tên đăng nhập và IP
whoami
Get-NetIPAddress -AddressFamily IPv4 | Where-Object PrefixOrigin -eq Dhcp
```

`whoami` ra `desktop-abc\viet` thì tên đăng nhập SSH là `viet`. Với tài khoản Microsoft, tên đăng
nhập là tên thư mục trong `C:\Users\`, mật khẩu là mật khẩu tài khoản Microsoft. Đặt DHCP
reservation cho máy Windows để IP không đổi.

**2. Thử bằng mật khẩu từ Mac:** `ssh viet@<WIN_IP>`.

**3. Chuyển sang key.** `ssh-copy-id` không dùng được với Windows, nên làm tay. Trên Mac:

```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519
scp ~/.ssh/id_ed25519.pub viet@<WIN_IP>:mac.pub   # vào thư mục home trên Windows
```

Trên Windows (PowerShell quản trị), trước hết xem tài khoản có thuộc nhóm Administrators không:

```powershell
([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')
```

Nếu `True` (thường gặp ở máy cá nhân): sshd **bỏ qua** `~\.ssh\authorized_keys` và chỉ đọc file
chung. Quyền file phải chỉ gồm Administrators và SYSTEM, nếu không sshd lặng lẽ bỏ qua key (SID
`S-1-5-32-544` thay cho chữ "Administrators" để chạy được cả trên Windows tiếng Việt):

```powershell
Get-Content $env:USERPROFILE\mac.pub | Add-Content $env:ProgramData\ssh\administrators_authorized_keys
icacls.exe "$env:ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "*S-1-5-32-544:F" /grant "SYSTEM:F"
```

Nếu `False`:

```powershell
New-Item -ItemType Directory -Force $env:USERPROFILE\.ssh
Get-Content $env:USERPROFILE\mac.pub | Add-Content $env:USERPROFILE\.ssh\authorized_keys
```

`ssh viet@<WIN_IP>` không còn hỏi mật khẩu là key đã chạy.

**4. Tắt đăng nhập bằng mật khẩu** (sau khi key đã chạy). `notepad $env:ProgramData\ssh\sshd_config`,
đặt `PasswordAuthentication no`, rồi `Restart-Service sshd`. Giữ sẵn một phiên SSH đang mở trước khi
restart để còn đường vào nếu cấu hình sai.

**5. Tên tắt trên Mac**, thêm vào `~/.ssh/config`, sau đó dùng `ssh win`, `scp file win:`:

```
Host win
  HostName <WIN_IP>
  User viet
  IdentityFile ~/.ssh/id_ed25519
```

**Điều khiển render worker qua SSH.** Không chạy `render-worker.ps1` trực tiếp trong phiên SSH: SSH
ngắt thì worker chết theo, và dòng đang render giữ `rendering` tới khi lease 75 phút hết hạn. Để
worker chạy bằng Task Scheduler (mục 5.6) và dùng SSH để bật, tắt, xem trạng thái (thay
`reup-render-worker` bằng tên task đã đặt):

```powershell
schtasks /Run /TN "reup-render-worker"
schtasks /End /TN "reup-render-worker"     # dừng cưỡng bức, dòng không về hàng đợi ngay
schtasks /Query /TN "reup-render-worker"
```

`render-worker.ps1` chỉ in log ra console, chưa ghi file, nên qua SSH chưa xem được log của task.

## 6. Kiểm chứng

1. Trên dashboard, duyệt một video thử, rồi trên Mac:

   ```sql
   select aweme_id, generation, state, attempts, error, final_key
   from localization.final_renders order by rendered_at desc nulls first limit 5;
   ```

   Kỳ vọng `queued` → `rendering` → `rendered`; log worker Windows có
   `render <aweme>/g<gen>: rendered`; log worker Mac **không** có dòng render nào.
2. Dashboard phát được bản cuối và SRT; poster `.jpg` nằm cạnh MP4 trong `output/review/...`.
3. So với một bản đã render trên Mac: cùng độ phân giải, fps, thời lượng, phụ đề giống (mốc cũ:
   SSIM khoảng 0,98 giữa render và Studio export; video 189 giây mất khoảng 2 phút 10 giây trên
   Mac). Ghi lại thời gian trên Windows để đo lợi ích thật, đồng thời thử vài giá trị
   `FINAL_RENDER_WORKERS`.
4. `select name, seen_at, info->'disk_free_bytes' from localization.host_workers;` thấy cả
   `review-worker` (Mac) và `render-worker-win`, `seen_at` mới.
5. Ctrl+C worker giữa lúc render: dòng về `queued`, `attempts` không tăng; chạy lại thì render tiếp.
   Thử thêm kill cưỡng bức: không còn tiến trình `chrome-headless-shell.exe` mồ côi sau khi worker
   khởi động lại và lease hết hạn.

## 7. Vận hành và rollback

- **Không tắt hẳn `reup-review-worker` trên Mac để dồn render sang Windows.** Tắt hẳn thì Mac
  ngừng nhận render, nhưng mất luôn nút "Mở Studio" trên dashboard, việc quét review mỗi 60 giây
  và heartbeat `review-worker`. Sau 2 phút badge worker chuyển đỏ, VoiceStudio và đĩa thành
  `unknown`; heartbeat `render-worker-win` không hiện trên dashboard. Đúng cách là chuyển Mac sang
  vai trò `studio` (mục 4, bước 6): Mac vẫn làm mọi việc khác, chỉ không nhận render.
- Trước khi Windows render thành công lần đầu, cứ để Mac ở `all`. Hai máy cùng nhận render vẫn
  an toàn: mỗi dòng chỉ một máy claim được (`FOR UPDATE SKIP LOCKED`), máy nào rảnh trước thì làm.
- Khi Mac ở `studio`, chỉ Windows render. Windows tắt thì dòng mới nằm `queued` mãi, không báo
  lỗi; muốn Mac làm thay thì làm theo mục rollback bên dưới.
- Đổi vai trò hoặc dừng worker Mac lúc đang render: worker đợi tối đa 60 giây, trả dòng về
  `queued` mà không tính lần thử, rồi Windows nhận lại ở vòng kế tiếp.
- Windows tắt hoặc mất mạng giữa render: dòng giữ `rendering` tới khi lease hết hạn (75 phút), rồi
  được nhận lại, tối đa 3 lần thử, sau đó `failed`. Dashboard không cho duyệt lại hay trả về khi đang
  `rendering`.
- Đĩa Windows dưới `REVIEW_WORKER_MIN_FREE_GB`: worker ngừng nhận việc và ghi lý do vào heartbeat.
- Thư mục tạm `data\review-render\final-render-*` có thể còn sót nếu Chrome giữ file khi xóa; worker
  tự dọn thư mục cũ hơn lease khi khởi động (`clean_stale`).
- Rollback về Mac: dừng tác vụ Windows, đặt lại `//` trước dòng `env` của `reup-review-worker`
  trong `ecosystem.config.cjs` (vai trò về `all`), rồi `pm2 delete reup-review-worker && pm2 start
  ecosystem.config.cjs --only reup-review-worker`. Không có migration hay dữ liệu cần hoàn tác. Muốn đóng LAN: gỡ
  `docker-compose.lan.yml` và `COMPOSE_FILE`, rồi `docker compose up -d --no-deps postgres minio`.
- Nâng cấp: cập nhật hai máy cùng commit. HyperFrames ghim `0.8.77`; đổi ghim phải xác minh lại trên
  cả hai máy.

## 8. Câu hỏi còn mở

- Bước 2 của mục 3.1 yêu cầu hai lần render thật trên Mac (trước và sau refactor) cho cùng kết
  quả; việc này chưa làm, cần duyệt một video thử trên dashboard sau khi `pm2 reload
  reup-review-worker`.
- `uv sync --no-dev` có cài được `torch==2.5.1` và `silero-vad` trên Windows không? Nếu không, cần
  tách các phụ thuộc này ra khỏi đường worker render.
- HyperFrames 0.8.77 render có chạy ổn trên Windows không? Chưa kiểm chứng; đây là rủi ro lớn nhất
  của hướng native và sẽ lộ ra ở lần render thật đầu tiên.
- Dashboard có cần hiện riêng worker render Windows (badge và đĩa) không?
- GPU của máy Windows là hãng nào (NVIDIA, AMD hay Intel)? Quyết định bộ mã hóa ffmpeg (mục 5.7).
- Cờ `--gpu` của HyperFrames 0.8.77 có tồn tại và chọn bộ mã hóa nào? Tài liệu công khai nhắc có
  cờ này nhưng không nói rõ; cần `hyperframes render --help` trên bản ghim.

Nguồn tham khảo về hỗ trợ Windows của HyperFrames: [hướng dẫn render chính thức](https://hyperframes.heygen.com/guides/rendering),
[bản phát hành v0.7.27 (sửa lỗi spawn trên Windows)](https://newreleases.io/project/github/heygen-com/hyperframes/release/v0.7.27),
[hướng dẫn cài trên Windows (bên thứ ba)](https://autoae.online/blog/hyperframes-topic2video-windows-setup).

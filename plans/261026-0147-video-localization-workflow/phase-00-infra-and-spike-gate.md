---
phase: 0
title: "Hạ tầng và spike gate"
status: in-progress
priority: P1
effort: "3d"
dependencies: []
---

# Phase 00: Hạ tầng và spike gate

<!-- Updated: Red Team 2026-10-26 - bảo mật port/role/ACL/digest, G2 đo đúng, G3 cross-language, G5 p10, thêm G6 -->

## Goal

Dựng hạ tầng mới với ranh giới bảo mật rõ ràng, rồi trả lời G1–G6 (plan.md §3) bằng bằng chứng
chạy được. Phase này chưa có code domain.

## Files to Create / Modify

- **Modify `docker-compose.yml`.** Thêm các service sau, mỗi image đều pin `@sha256:`:
  - `db-init`
  - `temporal`, `temporal-admin-tools`, `temporal-ui`
  - `minio`, `minio-init`
  - `clickhouse`
  - `langfuse-web`, `langfuse-worker`

  Thêm `--maxmemory-policy noeviction` và ACL file cho `redis`.
- **Create `infra/postgres/create-databases.sh`.** Idempotent, gồm:
  - Tạo role `temporal`, `langfuse`, `localization` với `NOSUPERUSER NOCREATEDB`.
  - Tạo database `temporal`, `temporal_visibility` (owner `temporal`) và `langfuse` (owner `langfuse`).
  - `REVOKE CONNECT ON DATABASE reup FROM PUBLIC`.
  - `GRANT CONNECT` trên `reup` cho `localization`. Grant chi tiết làm ở phase 02 và 03.
- **Create `infra/redis/users.acl`.**
  - User `default` cho downloader và parser, giữ nguyên quyền.
  - User `langfuse` chỉ được truy cập theo pattern key của Langfuse. Pattern xác định ở G4.
- **Create `infra/minio/init.sh`.**
  - Tạo bucket `reup-media` và `langfuse`.
  - Policy theo prefix cho từng service: `downloader`, `localization`, `renderer`, `langfuse` (xem plan.md §6).
  - Lifecycle `work/` hết hạn sau **30 ngày**. Không đặt lifecycle cho `cache/`, `review/`, `output/`.
- **Modify `.env.example`.** Thêm các biến của plan.md §6, không có giá trị thật. Gồm cả secret
  Langfuse (`NEXTAUTH_SECRET`, `SALT`, `ENCRYPTION_KEY`), root MinIO và password ClickHouse.
- **Modify `services/downloader/test/compose-constraints.spec.ts`.** Thêm hai assertion:
  - Không service mới nào bind `0.0.0.0`.
  - Mỗi service chỉ nhận đúng env của nó.
- **Create `services/localization/spikes/`.** Các file: `g1_durable_agent.py`, `g2_voicestudio.py`,
  `g3_trace.py` (kèm `g3_render_span.ts`), `g5_ocr_bbox.py`, `g6_history_size.py`, và `fixtures/g5-labels.json`.
- **Create `plans/261026-0147-video-localization-workflow/reports/gate-results.md`.**

## Tasks & Steps

### Chuẩn bị

1. **Backup trước mọi thay đổi:** `pg_dump -Fc reup > backups/reup-<timestamp>.dump`. Ghi đường dẫn vào `gate-results.md`.
2. **Chốt image bằng digest.**
   - Temporal: server, admin-tools, ui.
   - Langfuse v3: web và worker.
   - ClickHouse ≥ 24.3.
   - MinIO: ưu tiên bản Chainguard.

   Ghi digest vào `gate-results.md`.

### Port và secret

3. **Chỉ ba service publish port**, và đều bind `127.0.0.1`:
   - `temporal-ui` → `127.0.0.1:8233`
   - `langfuse-web` → `127.0.0.1:3100`, **không dùng 3000** vì trùng với `nest`
   - Console MinIO → `127.0.0.1:9001`

   Temporal frontend, ClickHouse và API MinIO không publish port nào.
4. **Secret và auth.**
   - Langfuse đặt `AUTH_DISABLE_SIGNUP=true`.
   - Root MinIO và password ClickHouse không được để mặc định. `minio-init` từ chối chạy nếu thấy `minioadmin`.

### Dựng hạ tầng

5. **`db-init`.**
   - Đặt `restart: "no"`.
   - Các service phụ thuộc bằng `service_completed_successfully`.
   - **Không** dùng `/docker-entrypoint-initdb.d`, vì volume đã có dữ liệu nên thư mục đó không chạy.
6. **Temporal.**
   - Dùng `temporal-sql-tool` để setup và update schema, kết nối bằng role `temporal`.
   - Đặt retention cho namespace `default` là 30 ngày.
   - Bật SQL visibility.
7. **Langfuse.**
   - Kết nối DB bằng role `langfuse`.
   - Redis dùng user ACL `langfuse`.
   - S3 dùng key riêng.
   - `TZ=UTC` cho Langfuse, ClickHouse và Postgres.
   - Giới hạn memory cho ClickHouse.
8. **Env allowlist.** Mỗi service chỉ nhận đúng biến của nó, theo văn hoá của service `nest`.

### Chạy các gate

9. **G1.** Spike agent với tool `count_call`. Tool ghi `UPSERT spike_calls(run_id, call_no)`.
   - Kill worker bằng `SIGKILL` ngay sau `ActivityTaskCompleted` của tool, xác nhận qua
     `temporal workflow show`. Sau đó start lại worker.
   - Kỳ vọng: mỗi `call_no` chỉ có một dòng, và không có activity model nào chạy lại.
   - **Ghim tool vào task queue `io`** qua config activity theo từng tool của `TemporalAgent`.
     Kiểm trong history rằng activity đó chạy trên `io`.
   - Ghi lại các model OpenRouter đã thử, và model nào gọi tool được.
10. **G2.** Chạy trên 3–5 video trong `data/`, với `whisperx` và `funasr`. Nếu người dùng có key
    thì thêm một API STT cloud tương thích OpenAI.
    - **Đo tỷ lệ từ thiếu timestamp đúng cách:** so ký tự trong `text` với ký tự gộp từ `words`.
      VoiceStudio âm thầm bỏ những từ không có timestamp, nên đếm trực tiếp sẽ luôn ra 0.
    - Gán tay 10 câu, độ lệch timestamp ≤ 200ms là đạt.
    - `GET /v1/audio/voices` phải chứa `VOICE_ID`.
    - Gọi `/v1/audio/speech`, rồi đo bằng `ffprobe` để tính số âm tiết mỗi giây, dùng bộ đếm D6 trên câu có số.
    - Gửi request **không có key** thì phải nhận 401. Ghi lại scheme của URL (https hoặc IP mạng riêng).
    - Ghi engine ASR đang active, vì nó đi vào cache key.
11. **G3.**
    - Một workflow có một activity Python và một activity `render` giả trên worker Node.
    - Bật `TracingInterceptor` của Python, interceptor OTel của TS và exporter OTLP tới Langfuse.
    - Kỳ vọng:
      - Đúng một trace.
      - Có span của worker Node.
      - Kill rồi resume thì **số span không đổi**. Điều này cho thấy không có span sinh từ code
        workflow lúc replay.
12. **G4.**
    - Diff `pg_dump --schema-only reup` trước và sau phải rỗng.
    - `psql -U langfuse -d reup` bị từ chối.
    - `client.count_workflows('ExecutionStatus="Running"')` chạy được.
    - Xác định pattern key Redis của Langfuse, rồi siết ACL theo pattern đó. Nếu Langfuse không
      dùng prefix cố định thì tách Redis riêng cho Langfuse, và ghi quyết định này lại.
    - `compose-constraints.spec.ts` xanh.
13. **G5.**
    - 20 frame lấy từ 5 video, bbox gán tay.
    - Mỗi model ghi quy ước bbox của nó vào `OCR_BBOX_FORMAT`.
    - Đạt khi IoU trung vị ≥ 0.7 **và** p10 ≥ 0.5.
14. **G6.**
    - Chạy một Localizer giả (tool thật đọc transcript của video 3 phút) qua `TemporalAgent`.
    - Đọc history, lấy kích thước input lớn nhất của các activity model-request.
    - Kỳ vọng < 200KB. Nếu vượt thì ghi lại rằng phase 05 phải chia theo cửa sổ 60 giây.
15. **Ghi kết quả.** `gate-results.md` có PASS/FAIL, bằng chứng, lệnh tái hiện, và phương án thay thế nếu fail.

## Verification

- `docker compose ps`: mọi service healthy. `db-init` và `minio-init` exit 0, và chạy lần thứ hai vẫn exit 0.
- `pnpm --filter @reup-flow/downloader test -- compose-constraints`: xanh.
- Spike G1–G6 exit 0. `gate-results.md` có đủ 6 dòng.
- `docker compose config | grep -E '0\.0\.0\.0|"[0-9]+:[0-9]+"'`: không có port mới nào thiếu `127.0.0.1`.

## Risk

- Thiếu `VOICESTUDIO_URL` thì G2 bị chặn, các gate khác vẫn chạy.
- VoiceStudio chưa bật `OMNIVOICE_API_KEY` thì G2 FAIL. Người dùng phải bật auth trên máy đó.

## Rollback

1. Gỡ các service mới.
2. Drop các database và role mới.
3. `GRANT CONNECT ON DATABASE reup TO PUBLIC` để trả lại quyền cũ.
4. Restore `reup` từ backup nếu cần.

## Tiến độ (cập nhật 2026-10-26)

Kết quả đầy đủ, bằng chứng và lệnh tái hiện: [reports/gate-results.md](./reports/gate-results.md).
Code nằm trên nhánh `feat/localization-phase-00-infra` (3 commit, chưa push).

**Đã xong**
- [x] Hạ tầng compose: `db-init`, Temporal (+ `temporal-admin-tools`, `temporal-namespace-init`, `temporal-ui`), MinIO (+ `minio-init`), ClickHouse, Langfuse web và worker. Healthy, init job chạy lại vẫn exit 0.
- [x] Role Postgres riêng, ACL Redis cho Langfuse (pattern `bull:*`, `langfuse:*`, `api-key:*`), policy MinIO theo prefix, lifecycle `work/` 30 ngày, retention namespace 30 ngày.
- [x] `compose-constraints.spec.ts` (17 test) và harness `phase-infra.sh` (11 mutation, 2 control) đã đăng ký trong `scripts/run-mutations.sh`.
- [x] G1 PASS (scripted và 2 model OpenRouter thật), G3 PASS, G4 PASS, G6 PASS (transcript tổng hợp).

**Chưa xong**
- [x] **G2:** đã chạy 2026-10-26 trên VoiceStudio thật. Auth 401, voice, TTS và STT (với `language=zh`) đạt; hand-align 9/10 câu trong 200 ms, người dùng chấp nhận mức này. Còn lại cho các phase sau: STT phải gửi `language`, phản hồi không có tên engine, server chưa có giọng tiếng Việt. Chi tiết ở `reports/gate-results.md`.
- [ ] **G5:** cần `OCR_BBOX_FORMAT` và 20 frame gán nhãn tay từ 5 video **có sub cứng** (các video mẫu đã thử không có) trong `services/localization/spikes/fixtures/g5-labels.json`.
- [ ] G6 đo lại với transcript thật khi có STT.
- [ ] `.env` còn để trống `JUDGE_MODEL` và `OCR_BBOX_FORMAT`.

**Quyết định của người dùng (2026-10-26):** triển khai các phase 01 trở đi trước, tạm bỏ qua G2 và G5, quay lại chạy các gate đó khi VoiceStudio sẵn sàng rồi chỉnh các phase sau nếu cần. Phase chịu ảnh hưởng: 04 (bước 2b `detect_subtitles`, G5), 05 (hằng số tốc độ đọc D6, G2), 06 và 08 (VoiceStudio thật, G2). Mọi phần phụ thuộc phải nằm sau interface provider, không viết cứng giả định.

## Quay lại thì bắt đầu như sau

1. `git checkout feat/localization-phase-00-infra`, rồi `docker compose up -d --wait` (không bật `nest`, `parser`, `web` nếu không cần: `nest` chạy lane tải video).
2. Điền các biến còn thiếu vào `.env` (không chép dấu `<>` hay comment từ mẫu).
3. Chạy từ `services/localization/spikes` sau `set -a; . ../../../.env; set +a`:
   - G2: `./run.sh uv run python g2_voicestudio.py`
   - G5: `./run.sh uv run python g5_ocr_bbox.py`
4. Ghi kết quả vào `reports/gate-results.md`, đối chiếu từng phase đã làm ở danh sách ảnh hưởng ở trên và sửa nếu lệch. Chỉ khi cả sáu gate xong mới đổi `status` của phase này sang `completed`.

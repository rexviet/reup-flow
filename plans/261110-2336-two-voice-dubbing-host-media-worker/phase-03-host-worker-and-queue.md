---
phase: 3
title: "Host worker, queue, quyền và vòng đời process"
status: pending
priority: P1
effort: 2.5d
dependencies: [1, 2]
---

# Phase 3: Host worker, queue, quyền và vòng đời process

<!-- Updated: Red Team Review 2026-11-10 - Vbee giữ INGEST/media, schedule_to_start_timeout, MinIO init, supply chain, vòng đời process -->

## Goal

`ingest` (Demucs) của pipeline VoiceStudio chạy ở worker trên host qua queue `host`; Vbee giữ nguyên queue `media`. Chạy sạch và dừng sạch, không để process hay cổng mồ côi. Người dùng đã chấp nhận Temporal và MinIO ở loopback không xác thực.

## Files

- Modify `services/localization/src/localization/workers/run.py`: role `host` (queue `host`, `limit=1`; activities `ingest`; Phase 4 thêm `diarize`); **giữ `ingest` ở role `media`** vì Vbee dùng; cập nhật thông báo lỗi role (`workers/run.py:121,196-197`).
- Modify `workflows/video_localization.py`: thêm `INGEST_HOST` (task_queue `host`, `schedule_to_start_timeout` ngắn, `start_to_close` và `heartbeat` như `INGEST`) và `DIARIZE` (xem Phase 4). Dùng `INGEST_HOST` ở mọi chỗ gọi `ingest` của VoiceStudio (`_ingest`, `_ingest_beside_rate`, `_settle`, đường regenerate trong `_dub_with_regeneration`) sau patch marker `HOST_INGEST_PATCH`. `workflows/vbee_dubbing.py` không đổi.
- Modify `providers/demucs.py` và `providers/registry.py`: thêm `DEMUCS_DEVICE` (truyền `-d`); allowlist env cho child thêm biến cần cho MPS. `DEMUCS_BIN` đã có (`config.py:149`), trên host trỏ wrapper `demucs_safe`.
- Config: **không** thêm `HOST_S3_*` hay địa chỉ Temporal. Host worker dùng lại `TEMPORAL_ADDRESS`, `S3_ENDPOINT` và `LOC_S3_*` với giá trị riêng đặt trong `data/host-worker/env` (mode 0600; thư mục `data/` đã bị ignore).
- Modify `infra/minio/init.sh` (ở gốc repo, không nằm trong `services/localization`) và env của `minio-init` trong `docker-compose.yml`, `.env.example`: policy `host-worker` gồm Get trên `source/*` và `work/*`; Put, `AbortMultipartUpload`, `ListMultipartUploadParts` trên `work/*` (cùng mẫu policy hiện có, `init.sh:45-62`); guard identity phải khác identity `localization` (mẫu `init.sh:80-92`). Không có quyền Delete.
- Create `docker-compose.host-worker.yml`: publish `127.0.0.1:7233` (temporal) và `127.0.0.1:9000` (minio); service `worker-host` profile `host-fallback` chạy `workers.run host` **chỉ phục vụ `ingest`** (image không có pyannote, nên `diarize` không đăng ký ở đây).
- Modify `pyproject.toml` và `uv.lock`: dependency group `host` (pyannote nhánh đã chọn ở Phase 1).
- Create `scripts/host-worker.sh`: `uv sync --frozen` từ commit hiện tại; đặt `PATH` cố định (ffmpeg/ffprobe đã chọn, `activities/ffmpeg.py:93,145` gọi theo PATH); tải weights Demucs vào `TORCH_HOME` riêng và kiểm cùng sha256 như `Dockerfile`; từ chối chạy khi có `HF_TOKEN` trong môi trường, ép `HF_HUB_OFFLINE=1`; ghi PID và PGID vào `data/host-worker/`.
- Subprocess cho pyannote: `providers/diarizer.py` (Phase 4) chạy pyannote trong subprocess riêng với allowlist env như Demucs (`providers/demucs.py:11-29`), pin revision model và kiểm sha256 checkpoint, để cancel kill được và secret của worker không vào process model.
- Tests: `tests/workers/test_agent_wiring.py` (thêm role `host` vào `_served_by_production`, dòng 393-396 và 437-440; role `media` vẫn có `ingest`); `tests/replay/` (ghi history mới, history cũ vẫn pass); `tests/workflows/test_vbee_dubbing.py` (Vbee vẫn `ingest` ở `media`); test policy MinIO theo mẫu `tests/review_bridge/test_permissions.py`; thêm canary cho identity host và `HF_TOKEN` vào `tests/test_secret_canary.py`.

## Steps

1. Role `host`, `INGEST_HOST`, patch marker; Vbee không đổi.
2. `DEMUCS_DEVICE` và wrapper `demucs_safe` trên host.
3. Policy MinIO, guard identity, test policy.
4. `docker-compose.host-worker.yml`, `scripts/host-worker.sh`, process tracking theo `~/.claude/rules/process-management.md`: kiểm port và PID cũ trước khi chạy, không tăng port, ghi PGID của Demucs và pyannote.
5. Replay và test wiring.

## Validation

- `uv run pytest -q tests/workers tests/workflows tests/replay tests/test_secret_canary.py tests/test_demucs_safe.py tests/test_dockerfile.py`.
- Live (`live`): một video chạy `ingest` ở host worker, stems vào MinIO; chạy lại với `worker-host` trong Docker cho cùng kết quả.
- Tắt host worker rồi cho một run mới: run **fail rõ ràng** sau `schedule_to_start_timeout` với thông báo nêu queue `host`, không treo.

## Risk / rollback

- Rủi ro đã chấp nhận: Temporal/MinIO loopback không xác thực; ffmpeg parse media trên host. Giảm nhẹ như trên và ghi vào tài liệu bảo mật (Phase 7).
- Rollback: bật `worker-host` trong Docker, hoặc bỏ patch marker cho run mới.

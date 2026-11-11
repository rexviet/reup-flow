---
phase: 7
title: "start-all, tài liệu, kiểm thử live"
status: pending
priority: P2
effort: 1d
dependencies: [3, 4, 5, 6]
---

# Phase 7: start-all, tài liệu, kiểm thử live

<!-- Updated: Red Team Review 2026-11-10 - stop phải đóng cổng, nạp đủ file compose, dọn process, hiển thị commit -->

## Goal

Một lệnh bật và tắt đủ Docker, bridge, watcher và host worker; không để process hay cổng mồ côi; tài liệu cập nhật; có bằng chứng live.

## Files

- Modify `scripts/start-all.sh`:
  - Nạp đủ file compose một cách tường minh: `-f docker-compose.yml -f docker-compose.override.yml -f docker-compose.host-worker.yml` (thêm `-f` làm rơi tự động nạp override, file đang đặt `LOCALIZATION_DELIVERY_MODE=review_project`).
  - `start`: build image và `uv sync --frozen` từ cùng một commit, bật host worker, chờ queue `host` có poller trước khi báo ready.
  - `stop`: dừng host worker theo PID/PGID đã ghi (kill cả nhóm Demucs và pyannote, chỉ process của mình), rồi tạo lại `temporal` và `minio` **không kèm** file host-worker để đóng 7233 và 9000.
  - `status`: hiển thị host worker, `docker port` của `temporal`/`minio`, commit của image và của môi trường host (cảnh báo khi lệch).
- Docs: `README.md` (biến `VOICE_ID_MALE`, `VOICE_ID_FEMALE`, `DEMUCS_DEVICE`, cách bật host worker, token HF chỉ cần một lần để tải model, thứ tự deploy của Phase 2); `docs/review-companion.md` (đổi voice từng cue); tài liệu bảo mật ghi rõ: Temporal/MinIO loopback không xác thực là rủi ro đã chấp nhận, identity MinIO riêng của host worker, file env mode 0600, ffmpeg chạy trên host với `PATH` cố định.
- Không có bước dọn `ingest` khỏi role `media`: Vbee dùng vĩnh viễn.
- Báo cáo kết quả: `plans/reports/` kèm bằng chứng live.

## Validation

- `scripts/start-all.sh start`, `status`, `stop` chạy thật. Sau `stop`: `lsof -i :7233 -i :9000` rỗng; `pgrep -f demucs` và `pgrep -f pyannote` rỗng; `docker compose ps` không còn service của dự án.
- Dừng giữa lúc đang tách một video: không còn Demucs hay ffmpeg mồ côi; lần `start` sau không chạy chồng hai Demucs.
- Live một video nam nữ từ downloader tới Studio: hai voice đúng câu, đổi voice một cue được.
- Test mặc định (không `live`): `uv run pytest -q` trong `services/localization`; `node --test scripts/tests` cho phần script.
- Kiểm tra thuật ngữ cũ không còn trong plan: `grep -rnE "HOST_S3|voice_override|SpeakerDocument\b|bridge cũ bỏ qua" plans/261110-2336-two-voice-dubbing-host-media-worker/*.md` chỉ được khớp trong bảng Red Team Review.

## Risk / rollback

`start-all.sh` đã tồn tại và mới chỉ kiểm cú pháp; lần chạy thật đầu tiên là ở phase này, nên chạy `status` trước và `start --no-host` trước khi bật host worker.

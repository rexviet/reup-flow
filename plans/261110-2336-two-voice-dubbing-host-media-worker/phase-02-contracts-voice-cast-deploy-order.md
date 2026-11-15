---
phase: 2
title: "Contract, voice cast và thứ tự deploy"
status: pending
priority: P1
effort: 1.5d
dependencies: [1]
---

# Phase 2: Contract, voice cast và thứ tự deploy

<!-- Updated: Red Team Review 2026-11-10 - voice_cast lên trước để diarize có cổng xác định; thứ tự deploy vì extra=forbid -->

## Goal

Mọi contract mới nằm sẵn ở mọi process trước khi có gì sinh ra chúng. `voice_cast` là cổng duy nhất bật diarization và voice kép.

## Files

- Modify `services/localization/src/localization/contracts/voice.py`: `VoiceCast` (`default: VoiceRef`, `male: VoiceRef | None`, `female: VoiceRef | None`) và `voice_for(cast, gender) -> VoiceRef` (không có giới tính thì `default`).
- Modify `contracts/models.py`: `LocalizationInput.voice_cast: VoiceCast | None = None` (`voice` giữ nghĩa voice mặc định); `SpeakerTurnsDocument` (các turn `speaker, start_ms, end_ms` và `gender` theo speaker); `SpeakersDocument` (segment_id -> speaker, gender, kèm `transcript_sha256`). Chỉ trường tuỳ chọn.
- Modify `contracts/review_project.py`: `ReviewProjectManifest.voice_cast` tuỳ chọn (cạnh `voice`, dòng 66) và `ReviewCue.voice_role: Literal["default","male","female"] | None` (dòng 28-41).
- Modify `config.py` (`voice_id_male`, `voice_id_female`, hàm dựng `VoiceCast`; không dựng khi `localization_pipeline == "vbee"`) và `starter/outbox_starter.py` (dựng `voice_cast`; kiểm voice nam/nữ tồn tại cùng chỗ đang kiểm `VOICE_ID`, dòng 163-167 và 245-256).
- Modify `docker-compose.yml`: thêm `VOICE_ID_MALE`, `VOICE_ID_FEMALE` vào env của `localization-starter` (cạnh dòng 460-462); `.env.example` (cạnh dòng 185-191).
- Tests: `tests/contracts/`, `tests/test_contract_hardening.py`, `tests/starter/`.

## Thứ tự deploy (bắt buộc, ghi vào tài liệu Phase 7)

1. Build mọi thứ từ cùng một commit: image Docker (agents, media, bridge) và môi trường Python trên host (`uv sync --frozen`).
2. Khởi động lại toàn bộ consumer: `worker-agents`, `worker-media`, `review-bridge`, host worker.
3. Chỉ sau đó mới đặt `VOICE_ID_MALE`/`VOICE_ID_FEMALE` và khởi động lại `localization-starter`. Starter cũ không biết `voice_cast`; worker cũ gặp `voice_cast` sẽ lỗi `ValidationError`.
4. Rollback: bỏ hai biến và khởi động lại starter; contract mới vẫn nằm yên, vô hại.

## Steps

1. Contract và test: JSON cũ decode; input không có `voice_cast` cho hành vi cũ; `voice_for` với giới tính thiếu rơi về `default`.
2. Starter: dựng `voice_cast` chỉ khi đủ cả hai voice; kiểm tồn tại; không dựng cho Vbee.
3. Compose và `.env.example`.

## Validation

- `uv run pytest -q tests/contracts tests/test_contract_hardening.py tests/starter`.
- Chạy `LocalizationInput` có `voice_cast` qua build cũ để chứng minh nó bị từ chối (xác nhận lý do cần thứ tự deploy), rồi qua build mới thì pass.

## Risk / rollback

Chỉ thêm trường tuỳ chọn; rollback bằng cách bỏ biến môi trường. Rủi ro lệch phiên bản được xử lý bằng thứ tự deploy và bước hiển thị commit ở Phase 7.

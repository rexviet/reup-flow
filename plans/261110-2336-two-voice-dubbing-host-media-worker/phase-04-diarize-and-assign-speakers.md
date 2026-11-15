---
phase: 4
title: "Diarization hai bước và gán speaker"
status: pending
priority: P1
effort: 1.5d
dependencies: [2, 3]
---

# Phase 4: Diarization hai bước và gán speaker

<!-- Updated: Red Team Review 2026-11-10 - diarize chỉ trả turn; gán speaker chạy sau transcript và tính lại khi transcript đổi -->

## Goal

Speaker và giới tính của mỗi segment luôn khớp transcript hiện tại, kể cả khi transcript được tạo lại. Lỗi hoặc vắng mặt của diarization không làm hỏng run.

## Thiết kế

1. `diarize(spec, vocals_key)` chạy trên queue `host` song song `transcribe`; chỉ trả key của `SpeakerTurnsDocument` (turn và giới tính theo speaker). Không cần transcript.
2. `assign_speakers(spec, transcript_key, turns_key)` chạy trên queue `io` sau khi có transcript: ghép turn với `SttSegment` bằng chồng lấp lớn nhất, ghi `SpeakersDocument` kèm `transcript_sha256`. Người tiêu thụ bỏ qua tài liệu có hash lệch transcript. Khi `transcribe` chạy lại (`_rerun_input_stage`), `assign_speakers` cũng chạy lại.
3. Không thêm tham số cho activity cũ: `assemble_script` và các bước sau đọc `SpeakersDocument` qua key suy ra bằng `work_key_for(spec, round, "speakers.json")` (`storage/object_store.py:49`), cùng quy ước đặt tên artifact work của `ingest`/`transcribe`; `require_work_key` (`object_store.py:59`) kiểm key thuộc run.

## Files

- Create `services/localization/src/localization/providers/diarizer.py`: giao diện `Diarizer` và bản pyannote chạy trong subprocess (xem Phase 3); giới tính theo cách Phase 1 chọn.
- Create `activities/diarize.py` (activity `diarize`) và `activities/assign_speakers.py`.
- Modify `workers/run.py`: `diarize` ở role `host`; `assign_speakers` ở queue `io` của role `agents`.
- Modify `workflows/video_localization.py` (`_prepare`, quanh dòng 554-594): patch marker `DIARIZE_PATCH`, chỉ khi `spec.voice_cast is not None`. Handle của `diarize` **ngoài** dict `stages` (để lỗi không kích hoạt `FIRST_EXCEPTION`/huỷ `transcribe`, dòng 580-593), có options `DIARIZE` riêng: `schedule_to_start_timeout` vài phút, `heartbeat_timeout`, retry thấp. Bắt `ActivityError`/timeout, ghi cảnh báo, rơi về voice mặc định **và** kết thúc run ở `needs_human` kèm lý do "chưa tách được người nói" (quyết định validation; cơ chế đặt `needs_human` lấy từ luồng đang có ở `activities/review_project.py`, xác nhận khi làm). Chờ kết quả `diarize` rồi gọi `assign_speakers` trước `run_localizer` (Localizer cần gender, Phase 5).
- Modify `activities/assemble_script.py` và `activities/script_document.py`: mang `speaker`/`gender` vào dòng script, trường tuỳ chọn, đọc từ `SpeakersDocument` hợp lệ.
- Tests: `tests/activities/test_diarize.py`, `tests/activities/test_assign_speakers.py` (chồng lấp lớn nhất, speaker không phủ segment, một speaker, thoại ngắn, hash lệch bị bỏ qua), `tests/contracts/`, `tests/workflows/` và `tests/replay/` (diarize lỗi trong lúc transcribe vẫn chạy; vắng worker `host` cho voice mặc định; transcript tạo lại thì speaker tính lại).

## Validation

- `uv run pytest -q tests/activities tests/contracts tests/workflows tests/replay`.
- Tắt host worker rồi chạy một video có `voice_cast`: run `ingest` fail rõ ràng (Phase 3); khi chỉ `diarize` vắng, run hoàn tất bằng voice mặc định và ở `needs_human`.
- Live: video nam nữ cho `speaker`/`gender` đúng ở tỉ lệ ghi trong báo cáo Phase 1; video một người không đổi; tắt `voice_cast` thì diarize không được gọi.

## Risk / rollback

Thiếu patch marker thì run đang chạy vỡ replay, nên replay test là bắt buộc. Rollback: bỏ cấu hình voice nam/nữ (Phase 2), `diarize` không được gọi.

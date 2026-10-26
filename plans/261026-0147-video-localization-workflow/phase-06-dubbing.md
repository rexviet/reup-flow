---
phase: 6
title: "Lồng tiếng: synthesize_all, Dubber, assemble_audio"
status: pending
priority: P1
effort: "2d"
dependencies: [5]
---

# Phase 06: Lồng tiếng

<!-- Updated: Red Team 2026-10-26 - cache cache/tts không lifecycle, key đủ trường, khử trùng trước batch, tool của Dubber ghim tts, synthesize_text, stretch_ratio cho mọi segment, AC1 viết lại, metadata text_vi -->

## Goal

Từ `LocalizedScript`, tạo ra `dubbed.wav`, và lưu script phiên bản sau Dubber. Mỗi hash TTS chỉ
gọi provider tối đa một lần (AC1, đã viết lại).

## Files to Create / Modify

- `src/localization/activities/synthesize.py`: `synthesize_segments` và `synthesize_text`, dùng chung hàm `tts_hash`.
- `src/localization/agents/dubber.py`
- `src/localization/activities/assemble_audio.py`
- `src/localization/workflows/video_localization.py`
- `tests/activities/test_synthesize.py`
- `tests/agents/test_dubber.py`
- `tests/activities/test_assemble_audio.py`
- `tests/workflows/test_resume_tts.py`
- `tests/mutations/phase-06.sh` và dòng `EXPECT`.

## Tasks & Steps

1. **`tts_hash`.**
   - Công thức: `sha256(canonical_json({provider, model: TTS_MODEL, engine: <engine active từ health check>, voice_id, settings, text_vi}))`.
   - Engine được truyền vào activity qua input, do workflow lấy từ một activity `voice_profile` lúc đầu run.
2. **Bước 4 trong workflow.**
   - Tính hash cho mọi segment, **khử trùng** (một hash chỉ xuất hiện trong một batch), rồi chia batch 10 hash.
   - Mỗi batch chạy activity `synthesize_segments` trên queue `tts`.
3. **`synthesize_segments`.** Với mỗi hash:
   - `cache/tts/{h}.wav` đã tồn tại thì bỏ qua.
   - Chưa có thì gọi TTS, rồi `put` kèm metadata `text_vi` (base64) và `voice_hash`.

   Đo `duration_ms` cho từng segment, rồi tính `stretch_ratio`:

   | `duration/slot` | `stretch_ratio` |
   |---|---|
   | ≤ 1 | 1.0 |
   | (1, 1.15] | `duration/slot` |
   | > 1.15 | Để trống, chuyển cho Dubber |

   Gọi heartbeat sau mỗi hash.
4. **Dubber.** Chỉ chạy với những segment còn thiếu `stretch_ratio`.

   | Tool | Queue | Mô tả |
   |---|---|---|
   | `get_segment` | `io` | Lấy dữ liệu của segment |
   | `estimate_duration` | — | Tính thuần |
   | `synthesize(id, text_vi)` | **`tts`** | Gọi `synthesize_text(text_vi, voice_ref, engine)`, dùng chung cache |
   | `accept(id, stretch_ratio)` | — | Clamp trong [0.85, 1.15], ngoài khoảng thì `ModelRetry` |

   Mỗi segment tối đa 3 lần viết lại. Hết lượt thì accept với stretch 1.15 và gắn `overflow_unresolved`.
5. **Ghi script mới.**
   - Workflow ghi `script.v{n+1}.json` qua `persist_json`. File cũ không bị sửa.
   - Workflow ghi `dub-segments.json` với `stretch_ratio` cho **mọi** segment.
6. **`assemble_audio`.**
   1. Mỗi clip qua `atempo`, rồi `adelay`.
   2. `amix` để ra track giọng.
   3. Duck BGM bằng `sidechaincompress`.
   4. `loudnorm` hai lượt, đích `LOUDNESS_LUFS` và `TRUE_PEAK_DBTP`.
   5. Ghi `work/{id}/r{round}/dubbed.wav` và tính `audio_sha256`.
   6. Độ dài lệch quá ±50ms so với video thì non-retryable.
7. **Thứ tự.** Bước 6 và 4b (phase 07) chạy song song **sau bước 5**, và đều đọc `script.v{n+1}`.

## Verification

Chạy `uv run pytest tests/activities tests/agents/test_dubber.py tests/workflows/test_resume_tts.py`.

**Test AC1** (provider fake đếm số lời gọi theo hash)
1. Kill sau khi batch 2/5 xong. Mỗi hash được gọi đúng một lần.
2. **Kill trong khoảng giữa lúc provider trả lời và lúc `put`.** Inject bằng một hook `before_put`
   gọi `os.kill`. Chỉ các hash đang chạy dở được gọi lại. Số đó không vượt `TTS_CONCURRENCY`, và
   test liệt kê từng hash.
3. Script có hai segment trùng text thì chỉ có một lời gọi.
4. Đổi `TTS_MODEL` hoặc engine thì cache miss.

**Test AC6:** TTS do Dubber gọi được tính vào overlap của queue `tts`.

**Các test khác**
- `accept(1.3)` bị từ chối.
- LUFS trong ±1 LU so với đích.
- Object TTS có metadata `text_vi` đúng.

**Mutation:** `bash scripts/run-mutations.sh localization/06` đỏ với:
- Bỏ cache.
- Bỏ khử trùng.
- Bỏ ghim queue `tts`.
- Bỏ clamp.
- `synthesize.py` import `agents`.

## Risk

`cache/tts/` lớn dần. Không đặt lifecycle vì AC1 phụ thuộc vào nó. Theo dõi dung lượng trong runbook.

## Rollback

Workflow dừng ở bước 3.

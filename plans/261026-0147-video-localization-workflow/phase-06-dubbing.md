---
phase: 6
title: "Lồng tiếng: synthesize_all, Dubber, assemble_audio"
status: in-progress
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

## Tiến độ

**Trạng thái: in-progress.** Code, test tự động, replay và mutation đã xong và xanh. Chưa đóng phase vì chưa chạy được với giọng và model thật: VoiceStudio không với tới được, `.env` chưa có `VOICE_ID`, `TTS_MODEL`, `STT_MODEL`, và VoiceStudio không có giọng tiếng Việt. Mọi số đo âm thanh dưới đây là trên audio sinh tại chỗ (tone, `ffmpeg`), không phải giọng đọc.

**Đã làm và đã kiểm**
- `activities/synthesize.py`: `tts_hash` (provider, model, engine, voice id, settings, text), `Synthesizer` (đọc cache `cache/tts/{h}.wav` rồi mới gọi provider; ghi kèm metadata `text-vi` base64, `voice-hash`, `duration-ms`), `synthesize_segments` (queue `tts`, heartbeat sau mỗi hash và heartbeat nền), `plan_synthesis` (khử trùng), `voice_profile`, bảng `stretch_for` và `final_stretch`. Hook `before_put` để test kill giữa lúc provider trả lời và lúc ghi.
- `activities/dub_script.py`: `measure_fit` (segment nào không vừa dù tăng tốc 1.15), `write_dubbed_script` (script phiên bản kế tiếp, bản cũ không bị sửa; câu viết lại mang liên lạc mà nguồn không có thì bị từ chối và dòng giữ nguyên chữ), `commit_dub` (ghi `dub-segments.json`, đủ mọi segment, kèm `overflow_unresolved`).
- `agents/dubber.py`, `agents/temporal.py`: Dubber là agent bền; model request ở `llm`, `get_segment` ở `io`, `synthesize` ghim `tts`, `estimate_duration` và `accept` chạy trong workflow. `accept` từ chối stretch ngoài [0.85, 1.15]; capability `RewriteLimit` từ chối lần viết lại thứ tư của một segment (đếm từ hội thoại nên replay cho cùng kết quả); văn bản video vào qua khối `<untrusted_video_text>`.
- `activities/assemble_audio.py` (queue `media`): `atempo` rồi `adelay` cho từng clip, `amix`, `sidechaincompress` duck BGM, `loudnorm` hai lượt tới `LOUDNESS_LUFS`/`TRUE_PEAK_DBTP`, độ dài lệch quá 50 ms so với video là lỗi không retry. Chạy được trong `ffmpeg` 5.1 của image `worker-media`.
- Workflow: lỗi của một batch được xử lý ngay khi xảy ra (không đợi các batch đứng trước nó trong danh sách xong) và các batch còn lại bị huỷ trước khi giai đoạn chạy lại; có test và mutation. Toàn bộ giai đoạn nằm sau `workflow.patched("dub-audio")`. Histories `phase-04` và `phase-05` vẫn replay; `phase-06` ghi mới (8 kịch bản, gồm Dubber chạy, mất stem nhạc, mất script). Một artifact mà giai đoạn đọc mà mất (`ingest`, `localize`, `dub_script`) thì stage sinh ra nó chạy lại đúng một lần (D10).
- Cấu hình: `LOUDNESS_LUFS`, `TRUE_PEAK_DBTP` (worker-media) và `DUBBER_MODEL` (worker-agents) trong `.env.example`, compose và allowlist của `compose-constraints.spec.ts`. Worker agents từ chối khởi động nếu thiếu `DUBBER_MODEL`.
- AC1 (test thật, SIGKILL thật, bucket là một thư mục để cả test và worker cùng nhìn): kill sau khi batch 2/5 xong (không hash nào bị gọi lại); kill trong khoảng giữa lúc provider trả lời và lúc `put` bằng hook `before_put` gọi `os.kill` (chỉ các hash đang chạy dở bị gọi lại, không vượt `TTS_CONCURRENCY`, test liệt kê từng hash); hai segment trùng text ở hai batch khác nhau chỉ một lời gọi; đổi `TTS_MODEL` hoặc engine thì cache miss; câu Dubber viết lại tốn đúng một lời gọi.
- AC6: TTS do Dubber gọi bị giới hạn bởi queue `tts` (giới hạn 2 thì đỉnh chồng nhau là 2; control giới hạn 4 thì là 4).
- `uv run pytest`: 693 passed, 1 skipped (6 live bị loại), ruff sạch. `bash scripts/run-mutations.sh 'localization/*' downloader/infra` xanh; harness `localization:06` có 54 mutation và 3 control (03: 11, 04: 47, 05: 57, infra: 16). Thêm code làm 4 mutation cũ của harness 04 và 05 không còn khớp (cancel sibling trong `_prepare`, model chưa gắn, hai `QueueSpec`); đã cập nhật cho khớp chứ không bỏ.

**Khác với phase file**
- Workflow không ghi `script.v{n+1}.json` bằng `persist_json`: workflow không giữ nội dung script, nên activity `write_dubbed_script` (queue `io`) đọc bản trước, áp quyết định của Dubber và ghi bản sau, như `assemble_script` ở phase 05. Luôn ghi bản mới, kể cả khi Dubber không chạy, để "phiên bản mới nhất" là một khái niệm duy nhất; `DubbedAudio.script_version` là 2.
- Workflow không tự tính hash: `plan_synthesis` (queue `io`) đọc script, tính và khử trùng, trả danh sách hash; payload chỉ mang hash và key. Sau Dubber có thêm một lượt `plan_synthesis` và `synthesize_segments` trên script mới (cache hit, trừ câu Dubber chưa đọc), để mọi text cuối cùng đều có clip trước `commit_dub`.
- `accept(id, stretch_ratio)` chỉ kiểm tra; quyết định (id, text cuối, stretch) là output có cấu trúc của agent, nên không có trạng thái nào nằm ngoài hội thoại. Stretch cuối cùng do `final_stretch` quyết, từ độ dài đo được của clip: không bao giờ thấp hơn mức cần để vừa slot, và segment vẫn quá dài ở 1.15 thì gắn `overflow_unresolved` dù Dubber đã accept.
- Tên metadata là `text-vi`, `voice-hash`, `duration-ms` (gạch nối): S3 và proxy gộp gạch dưới thành gạch nối. Phase 08 đọc `text-vi`.
- Ngoài danh sách file của phase: `activities/script_document.py`, `activities/dubber_ports.py`, `contracts/models.py` (các model của giai đoạn), `storage/object_store.py` (metadata), `activities/ffmpeg.py` (`run_ffmpeg`), `config.py`, `workers/run.py`, compose, `.env.example`, `compose-constraints.spec.ts`, `agents/temporal.py` (model theo alias cho hai agent).
- `DeferredModel` giờ tra model theo alias; `bind_model_factory(factory, alias)`.

**Chưa kiểm được (nói thẳng)**
1. **Chạy video thật: chưa làm.** Dubber chưa chạy với model thật (`DUBBER_MODEL` trong `.env` chưa thử), TTS chưa chạy với giọng tiếng Việt, nên chưa biết tỷ lệ segment không vừa slot, số lần Dubber viết lại, hay `vi_syllables` lệch bao nhiêu so với độ dài thật (hiệu chỉnh D6 vẫn mở từ phase 05).
2. **LUFS đo trên tone và nhiễu tổng hợp**, không phải giọng đọc cộng nhạc thật. `loudnorm` dạng tuyến tính có thể rơi về dạng động khi cần tăng gain quá nhiều so với true peak; trên giọng thật cần đo lại ±1 LU.
3. Histories replay vẫn từ activity giả.
4. Chưa có container nào chạy toàn bộ giai đoạn (stack docker không được khởi động cho phase này); chỉ kiểm filter graph của `assemble_audio` trong `ffmpeg` của image `worker-media`.

**Giới hạn đã biết**
- Run có `needs_human` từ Localizer vẫn được lồng tiếng (tốn TTS); phase 08 quyết định có dừng trước hay không.
- `cache/tts/` lớn dần, không lifecycle (theo thiết kế, AC1 phụ thuộc vào nó); runbook ở phase 10 phải ghi cách theo dõi dung lượng.
- Một câu dài quá ~1400 byte base64 không ghi được metadata (giới hạn 2 KiB của S3): lỗi không retry `TextTooLong`, trước khi tốn lời gọi TTS. Segment thật chỉ vài giây nên không gặp.
- Nếu worker chết giữa lúc provider trả lời và lúc `put`, đúng các hash đang chạy dở bị gọi lại (tối đa `TTS_CONCURRENCY`); đó là ngoại lệ đã khai của AC1.
- Dubber không biết các segment lân cận: một câu ngắn lại có thể làm khoảng lặng dài; chưa có chỉ số cho việc đó.
- Cổng `workflow.patched("dub-audio")` che toàn bộ giai đoạn; các đường fail mới trong giai đoạn đều nằm bên trong cổng.

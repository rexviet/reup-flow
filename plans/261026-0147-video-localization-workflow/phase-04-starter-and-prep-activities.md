---
phase: 4
title: "Starter, worker, replay test, tracing và các activity chuẩn bị"
status: completed
priority: P1
effort: "2.5d"
dependencies: [1, 2, 3]
---

# Phase 04: Starter, worker, replay test, tracing và các activity chuẩn bị

<!-- Updated: Red Team 2026-10-26 - generation, backoff, dead/rejected, kiểm điều kiện, throttle, tách worker workflow, concurrency mọi queue, replay test, D10, tracing không sinh span từ workflow -->

## Goal

Khi một video `done`, nó tự được start thành workflow `video-{aweme_id}-g{n}`, chạy qua bước 1,
2a và 2b. Một video lỗi không chặn hàng đợi. Backfill không làm ngập VoiceStudio hay CPU. Đổi code
workflow không làm vỡ các run đang chạy.

## Files to Create / Modify

**Starter và workflow**
- `src/localization/starter/outbox_starter.py`
- `src/localization/workflows/video_localization.py`
- `src/localization/activities/persist.py`: `persist_json(key, obj)`, để workflow không tự ghi S3.

**Activity**
- `src/localization/activities/ingest.py`
- `src/localization/activities/transcribe.py`
- `src/localization/activities/detect_subtitles.py`

**Worker và tracing**
- `src/localization/workers/run.py`: một process, nhiều `Worker`, mỗi `Worker` một task queue.
- `src/localization/workers/health.py`
- `src/localization/observability/tracing.py`

**Docker**
- `services/localization/Dockerfile`
  - Image được pin digest.
  - Có ffmpeg.
  - Weights Demucs tải sẵn lúc build và **kiểm sha256**.
  - Model được load với `weights_only=True`.
- `docker-compose.yml`: thêm `localization-starter`, `worker-agents`, `worker-media`, cùng env allowlist.

**Test**
- `tests/workflows/test_prep_stage.py`
- `tests/starter/test_outbox_starter.py`
- `tests/workers/test_queue_concurrency.py`
- `tests/replay/` cùng thư mục `histories/`
- `tests/mutations/phase-04.sh`
- `scripts/run-mutations.sh`: thêm dòng `EXPECT`.

## Tasks & Steps

1. **Starter.** Mỗi dòng được xử lý trong transaction riêng, không giữ khoá trong lúc gọi mạng.

   1. **Throttle.** Nếu `client.count_workflows('WorkflowType="VideoLocalizationWorkflow" AND ExecutionStatus="Running"') >= MAX_OPEN_WORKFLOWS` thì ngủ.
   2. **Claim một dòng:**

      ```sql
      UPDATE localization_outbox
         SET next_attempt_at = now() + interval '5 min'
       WHERE aweme_id = (SELECT aweme_id FROM localization_outbox
                          WHERE state='pending' AND next_attempt_at <= now()
                          ORDER BY next_attempt_at
                          LIMIT 1 FOR UPDATE SKIP LOCKED)
      RETURNING aweme_id, generation, dispatch_attempts;
      ```

      Sau đó commit.
   3. **Kiểm điều kiện.**
      - `storage_path` phải bắt đầu bằng `s3://`.
      - `aweme_id` và `channel_id` phải qua validator của phase 03.
      - Sai thì đặt `state='rejected'` kèm `last_error`.
   4. **Dựng input.** `LocalizationInput` có `target_lang` và `voice` **đọc lúc start** (AC5).
   5. **Start.** `start_workflow(id=f"video-{aweme_id}-g{generation}", task_queue="workflows", id_reuse_policy=REJECT_DUPLICATE, execution_timeout=14 ngày)`.
      - Gặp `WorkflowAlreadyStartedError` với cùng id thì coi là đã dispatch. Lúc này không còn
        nhập nhằng, vì mỗi generation có một id riêng.
   6. **Kết quả.**
      - Thành công: `state='dispatched'`, `dispatched_at`, `workflow_id`.
      - Thất bại: `dispatch_attempts+1`, backoff theo cấp số nhân qua `next_attempt_at`.
        Vượt `OUTBOX_MAX_ATTEMPTS` thì đặt `state='dead'`.
2. **Worker** (`workers/run.py`, entrypoint chọn queue theo lệnh).
   - `worker-agents`:

     | Queue | Giới hạn |
     |---|---|
     | `workflows` | Chỉ chạy workflow |
     | `llm` | `LLM_CONCURRENCY` |
     | `io` | Mặc định |
     | `stt` | `STT_CONCURRENCY` |
     | `tts` | `TTS_CONCURRENCY` |

   - `worker-media`: queue `media`, giới hạn `MEDIA_CONCURRENCY`.
   - Mỗi queue là một `Worker` độc lập. **Queue `workflows` không phụ thuộc VoiceStudio.**
   - Không có health check chặn lúc khởi động. Lỗi của VoiceStudio là lỗi tạm thời, retry có
     backoff (khoảng tối đa 5 phút).
   - `health.py` gọi `/.well-known/voicestudio-speech` mỗi 30 giây. Kết quả chỉ dùng để log và
     làm metric, đồng thời đọc engine đang active (dùng cho cache key ở phase 06).
3. **Workflow.**
   - Bước 1 chạy trên `media`.
   - Bước 2a (`stt`) và 2b (`io`) chạy song song.
   - Mỗi activity có `start_to_close_timeout`, `RetryPolicy` và `maximum_attempts` riêng.
   - Bắt `ArtifactMissingError` thì chạy lại stage sinh ra artifact **đúng một lần**. Thiếu lần
     thứ hai thì workflow fail (D10).
   - Mọi artifact được ghi qua activity, không có lần ghi nào từ code workflow.
4. **`ingest`.**
   - `ffprobe` lấy thông tin video. Không có stream video thì `no_video_stream`, non-retryable.
     Trường hợp này bắt được các bài dạng ảnh có nhạc nền MP3.
   - Demucs tách ra `vocals.wav` và `bgm.wav`, ghi vào `work/{id}/r0/`.
   - Gọi heartbeat trong lúc chạy.
5. **`transcribe`.**
   - Gọi STT và gom từ thành segment theo khoảng lặng > 400ms.
   - Ghi `transcript.json`.
6. **`detect_subtitles`.**
   - Lấy mẫu 2 frame mỗi giây, gửi theo lô 8 frame.
   - Chuẩn hoá bbox, lấy dải xuất hiện nhiều nhất làm `region`, rồi dựng `cues`.
   - Ghi `subtitles.json`.
   - Không có sub thì `region=None`.
7. **Tracing.**
   - `TracingInterceptor` gắn ở client lẫn worker. Exporter OTLP tới Langfuse.
   - **Không bật instrument của pydantic-ai trong code workflow.** Model request đã chạy trong
     activity, nên span được tạo ở đó, và tránh được span trùng khi replay (G3).
   - `session.id=aweme_id`.
8. **Replay.**
   - Sau phase này, lưu history của 3 run thật vào `tests/replay/histories/phase-04/`.
   - `Replayer` chạy lại toàn bộ history đã lưu trong CI.
   - Từ phase 05 trở đi, mọi thay đổi thứ tự command phải đi kèm `workflow.patched("<tên>")`.
   - Trước mỗi lần deploy phase mới, drain các run cũ (chờ xong, hoặc terminate kèm ghi log).

## Verification

Chạy `uv run pytest tests/workflows tests/starter tests/workers tests/replay`.

**Test starter**
- Hai starter chạy song song thì mỗi generation chỉ start một lần.
- Dòng có đường dẫn local bị `rejected` và **không chặn** dòng phía sau.
- Dòng lỗi 5 lần thì thành `dead`.
- Tăng `generation` cho một run đã completed thì có run mới (AC4).
- Đổi `VOICE_ID` giữa hai lần start: input của workflow cũ giữ nguyên (AC5).
- Có hơn `MAX_OPEN_WORKFLOWS` run đang chạy thì starter không start thêm.

**Test concurrency (AC6)**
- Activity fake của `stt`, `tts` và `media` ghi enter/exit kèm timestamp. Không được chồng nhau
  quá giới hạn đã đặt.
- **Control:** đặt giới hạn bằng 3 thì phải thấy chồng nhau.

**Test D10**
- Xoá `vocals.wav` trước bước 2a. `ingest` chạy lại đúng một lần, rồi workflow hoàn thành.

**Mutation**
- `bash scripts/run-mutations.sh localization/04` đỏ với các mutation sau:
  - Bỏ id xác định.
  - Không tăng generation.
  - Bỏ giới hạn concurrency.
  - Đảo thứ tự activity mà không có `patched` (replay).

**Thủ công**
- Bật stack rồi chèn một dòng outbox. Temporal UI thấy workflow chạy xong bước 2, và Langfuse thấy một trace.

## Risk

Demucs chạy CPU tốn khoảng 1× thời lượng video. `MEDIA_CONCURRENCY=2` bảo vệ RAM.

Outbox có dòng cho mọi video `done`, kể cả `storage_path` local; starter phải từ chối (`rejected`) path không phải `s3://`.

## Rollback

Dừng `localization-starter`. Outbox vẫn tích dòng, không mất gì.

## Tiến độ

Hoàn thành code và test 2026-10-26. Mục "Thủ công" (chèn dòng outbox, xem workflow chạy xong bước 2 trên Temporal UI và một trace trên Langfuse) **chưa chạy**: `.env` chưa có `VOICE_ID`, `STT_MODEL`, `TTS_MODEL`, `OCR_BBOX_FORMAT` (G5 chưa chạy) và `LOC_DATABASE_URL`, nên `worker-agents` và `localization-starter` không khởi động được. Đã kiểm thủ công phần còn lại: image build được, `worker-media` chạy từ compose và poll queue `media` trên Temporal thật, Demucs chạy trong image với `--network none` và `weights_only=True` trên một video thật.

Khác với plan:
- Harness `localization:04` có **26** mutation và 2 control (plan liệt kê 4 mutation). `downloader:infra` thêm 3 mutation (14 mutation, 2 control). `EXPECT` đã cập nhật.
- Allowlist import của `workflows/` thêm `temporalio.common`, `temporalio.exceptions`, `collections.abc` (retry policy, bắt `ActivityError`, `Callable`). Đây là các module thuần dữ liệu.
- `VOICE_ID` là cấu hình: starter kiểm nó có trong `GET /v1/audio/voices` của provider trước khi dispatch (server âm thầm rơi về preset nếu id lạ). Provider không trả lời thì starter tạm dừng, không đụng vào dòng. Vì vậy starter nhận thêm `VOICESTUDIO_URL`, `VOICESTUDIO_API_KEY`, `VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS` (bảng §6 chưa ghi). Thêm `VOICE_PROVIDER`, `VOICE_SETTINGS_JSON`, `TARGET_LANG`.
- `worker-media` không build provider nào ngoài Demucs, nên không cần cấu hình VoiceStudio hay OpenRouter. Queue `llm` và `tts` chưa có activity nên chưa có `Worker`.
- Demucs 4.0.1 chặn torchaudio dưới 2.1, mà `add_safe_globals` cần torch 2.4+. Image override đúng một ràng buộc đó (torch/torchaudio 2.5.1, CPU). Checkpoint chứa một số global ngoài model class (`Fraction`, numpy scalar và dtype, `_codecs.encode`); `demucs_safe` chỉ cho phép đúng các global liệt kê bằng `pickletools`.
- Cần grant cho role `localization` trên `videos` (SELECT) và `localization_outbox` (SELECT, UPDATE): `services/localization/migrations/261026-outbox-starter-grants.sql`. Chưa áp lên `reup`: cần `pg_dump` trước. Bảng outbox cũng chưa được áp lên `reup`.
- Histories trong `tests/replay/histories/phase-04/` là 3 lần chạy thật của workflow thật trên Temporal dev server, với activity giả (replay chỉ cần event). Ghi lại bằng `uv run python -m tests.replay.record_histories phase-04`. Chưa có history từ pipeline chạy provider thật; thêm khi chạy được bước thủ công.
- CI job `localization` cài thêm `ffmpeg`.

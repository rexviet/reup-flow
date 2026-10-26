---
phase: 4
title: "Starter, worker, replay test, tracing và các activity chuẩn bị"
status: pending
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

## Rollback

Dừng `localization-starter`. Outbox vẫn tích dòng, không mất gì.

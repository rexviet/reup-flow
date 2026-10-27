---
phase: 4
title: "Starter, worker, replay test, tracing và các activity chuẩn bị"
status: in-progress
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

**Trạng thái: in-progress.** Code, test tự động và mutation đã xong và xanh. Chưa đóng phase vì còn ba việc:

1. **Bước "Thủ công" chưa chạy.** `.env` chưa có `VOICE_ID`, `STT_MODEL`, `TTS_MODEL`, `OCR_BBOX_FORMAT` (G5 chưa chạy) và `LOC_DATABASE_URL`, nên `worker-agents` và `localization-starter` chưa từng khởi động. Cần chèn một dòng outbox, xem workflow chạy xong bước 2 trên Temporal UI và một trace trên Langfuse.
2. **Migration chưa áp lên `reup`.** Cả bảng `localization_outbox` lẫn `261026-outbox-starter-grants.sql` (grant theo cột cho role `localization`) mới chỉ được áp lên database tạm trong test. Cần `pg_dump` trước khi áp.
3. **Histories replay đến từ activity giả.** `tests/replay/histories/phase-04/` là 3 lần chạy thật của workflow thật trên Temporal dev server, nhưng activity là bản giả. Ghi lại từ một lần chạy với provider thật (`uv run python -m tests.replay.record_histories phase-04`) trước khi phase 05 đổi thứ tự command.

Đã kiểm thủ công: image build được, `worker-media` chạy từ compose và poll queue `media` trên Temporal thật, Demucs chạy trong image với `--network none` và `weights_only=True` trên một video thật.

Chưa kiểm được: giới hạn upload/thời lượng của STT trên VoiceStudio. Audio gửi STT được hạ xuống 16 kHz mono (nhỏ hơn khoảng 10 lần so với stem 44.1 kHz stereo) để giảm rủi ro, nhưng giới hạn thật vẫn cần xác nhận khi chạy bước thủ công với video dài.

Khác với plan:
- Harness `localization:04` có **42** mutation và 2 control (plan liệt kê 4). `downloader:infra` có 16 mutation, 2 control. `EXPECT` đã cập nhật.
- Allowlist import của `workflows/` thêm `temporalio.common`, `temporalio.exceptions`, `collections.abc` (retry policy, bắt `ActivityError`, `Callable`). Đây là các module thuần dữ liệu.
- `VOICE_ID` là cấu hình: starter kiểm nó có trong `GET /v1/audio/voices` trước khi dispatch (server âm thầm rơi về preset nếu id lạ), chỉ khi `VOICE_PROVIDER=voicestudio`. Thiếu `VOICESTUDIO_URL` với voicestudio thì starter từ chối dispatch (không fail open); provider khác thì dùng id nào cũng được. Provider không trả lời thì starter tạm dừng, không đụng vào dòng.
- Hàng đợi `io` có giới hạn `IO_CONCURRENCY` (mặc định 2). `detect_subtitles` trích frame JPEG đã thu nhỏ (cạnh dài nhất 1024) từng chunk 32 frame, không bao giờ ghi cả video ra đĩa. `/tmp` của worker là tmpfs có dung lượng giới hạn.
- Starter: lỗi hệ thống khi start (Temporal không với tới, bị từ chối, namespace) không tính vào `dispatch_attempts` nên không làm hàng thành `dead`; chỉ lỗi riêng của dòng mới tính. Ngưỡng `MAX_OPEN_WORKFLOWS` cộng thêm số workflow tiến trình này vừa start (visibility trễ). Chống tràn đó là ước lượng trong một tiến trình, không phải khoá nguyên tử giữa nhiều starter.
- Worker thoát với mã lỗi khi một `Worker.run()` chết (compose restart), có `graceful_shutdown_timeout` 60 s và `stop_grace_period` 90 s. Timeout Demucs tăng theo thời lượng media (2.5 lần, tối thiểu 600 s) và tiến trình Demucs bị kill khi activity bị huỷ. Upload stem có heartbeat.
- Demucs 4.0.1 chặn torchaudio dưới 2.1, mà `add_safe_globals` cần torch 2.4+. Image override đúng một ràng buộc đó (torch/torchaudio 2.5.1, CPU). Checkpoint chứa một số global ngoài model class (`Fraction`, numpy scalar và dtype, `_codecs.encode`); `demucs_safe` chỉ cho phép đúng các global liệt kê bằng `pickletools`.
- Grant theo cột: `videos(aweme_id, sec_user_id, storage_path)` chỉ đọc; `localization_outbox` đọc năm cột và chỉ update các cột sổ sách, không đổi được `generation`.
- `persist_json` chỉ ghi tài liệu `work/<video>/...json`.
- Giới hạn đã biết (không sửa ở phase này):
  - Một dòng gây lỗi "hệ thống" riêng của nó (DEADLINE_EXCEEDED, RESOURCE_EXHAUSTED, PERMISSION_DENIED) sẽ được thả lại mãi và mỗi lần làm starter ngủ 30 s; chỉ lỗi INVALID_ARGUMENT mới đưa dòng tới `dead`. Cần trần theo số lần liên tiếp hoặc theo tuổi nếu gặp trên thực tế.
  - Chống tràn `MAX_OPEN_WORKFLOWS` chỉ biết các lần start của chính tiến trình đó; hai starter cùng chạy với visibility trễ hơn 3 s có thể vượt giới hạn một chút. `RECENT_START_SECONDS=3` là ước lượng, cần đo độ trễ visibility thật.
  - `/tmp` của worker-agents là 512 MB: `detect_subtitles` tải cả video nguồn vào đó (nhân `IO_CONCURRENCY`), nên video 1080p dài 10 đến 15 phút có thể làm đầy (ENOSPC không được phân loại). tmpfs là RAM và các worker chưa có `mem_limit`.
  - Chưa làm: heartbeat khi một lô OCR chạy lâu (lô 8 request có thể bằng đúng `heartbeat_timeout`); G5 phải đo ở đúng kích thước 1024 px và JPEG đang dùng; video có metadata xoay chưa được xử lý; `persist_json` dùng chung queue `io` với `detect_subtitles`; `run_blocking` có thể in "Task exception was never retrieved" sau khi huỷ.
- Chưa làm (có chủ đích): gộp band phụ đề liền kề khi phát hiện bị tách đôi (cần dữ liệu thật, sau G5); quyết định video không có tiếng (hiện `no_audio_track` là lỗi vĩnh viễn, cần chốt với sản phẩm); khoá phụ thuộc Demucs bằng file hash và bỏ `curl` khỏi image runtime; reset `dispatch_attempts` khi tăng generation (chưa có thủ tục tăng generation trong code, thêm vào thủ tục đó); huỷ activity anh em khi một activity lỗi không phục hồi được; kết nối Postgres dùng lại giữa các vòng.
- CI job `localization` cài thêm `ffmpeg`.

## Chi phí OCR (đo trên 6 frame thật)

Frame 576x1024 JPEG, model `deepseek/deepseek-v4.1-flash` qua OpenRouter, đúng prompt của `VisionOcr`:
- mặc định: completion token [290, 732, 816, 255, 3280, 5472], trung bình $0.001235 mỗi lần gọi, 2 trong 6 câu trả lời nằm ngoài frame;
- `"reasoning": {"enabled": false}`: 16 completion token mỗi lần, trung bình $0.000067 mỗi lần gọi (rẻ hơn khoảng 18 lần), cả 6 box nằm trong frame;
- `"reasoning": {"effort": "low"}`: vẫn 224 đến 7271 token, trung bình $0.002231, tệ hơn nên không dùng.

Model tiêu hàng nghìn token suy luận để trả về JSON khoảng 25 token. Một lần chạy thật gọi OCR 55 lần cho một clip 23 s (46 trong `detect_subtitles`, 9 trong `review_render`), khoảng $0.10. Vì vậy mọi request OCR gửi `"reasoning": {"enabled": false}` và `"max_tokens": 200` (chỉ là chốt chặn cho câu trả lời chạy lan man), ở một chỗ duy nhất trong `providers/vision_ocr.py`. Câu trả lời rỗng hoặc bị cắt là trường hợp "không đọc được" cũ (lỗi vĩnh viễn, không retry). `SAMPLE_FPS` giữ 2.

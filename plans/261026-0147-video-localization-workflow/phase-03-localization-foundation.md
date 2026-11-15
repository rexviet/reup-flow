---
phase: 3
title: "Nền móng services/localization"
status: completed
priority: P1
effort: "2d"
dependencies: [0]
---

# Phase 03: Nền móng services/localization

<!-- Updated: Red Team 2026-10-26 - contract đầy đủ, codec guard, canary thật, allowlist đầy đủ, validate id, refactor mutation runner, VoiceRef đóng, kiểm URL VoiceStudio -->

## Goal

Dựng service Python với:
- Contract dữ liệu đầy đủ.
- Provider OpenAI-compatible thật.
- ObjectStore S3.
- Ba guard nền (nguyên tắc 1, 2, 3).
- Mutation runner chạy được harness đa ngôn ngữ.

Sau phase này, các phase sau chỉ thêm logic, không thêm hạ tầng.

## Files to Create / Modify

**Package và cấu hình**
- `services/localization/pyproject.toml`: uv, Python 3.12. Pin `temporalio` và `pydantic-ai-slim[openai,temporal]` theo kết quả G1.
- `src/localization/config.py`

**Contract và schema**
- `src/localization/contracts/`
- `contracts/edit-spec.schema.json`

**Storage và provider**
- `src/localization/storage/object_store.py`, kèm `ArtifactMissingError`.
- `src/localization/providers/`: `base.py`, `errors.py`, `openai_stt.py`, `openai_tts.py`,
  `vision_ocr.py`, `bbox.py`, `demucs.py`, `llm.py`, `registry.py`.

**Temporal**
- `src/localization/temporal/size_guard_codec.py`
- `src/localization/temporal/client.py`

**Migration**
- `services/localization/migrations/261026-localization-core.sql` và `.down.sql`.

**Test**
- `tests/architecture/test_workflow_imports.py`
- `tests/architecture/test_single_translation.py`
- `tests/test_size_guard_codec.py`
- `tests/test_secret_canary.py`
- `tests/test_input_validation.py`
- `tests/test_edit_spec_schema_drift.py`
- `tests/providers/`

**Mutation runner**
- Modify: `scripts/run-mutations.sh`.
- Create: `scripts/lib/mutation-protocol.sh`.
- Create: `services/localization/tests/mutations/phase-03.sh`.

## Tasks & Steps

1. **Contract.** Mọi model đều `frozen=True`.

   | Model | Field |
   |---|---|
   | `VoiceRef` | `provider: Literal[...]`, `voice_id`, `settings: <ProviderSettings>`. `ProviderSettings` là model đóng (`extra="forbid"`), ví dụ `VoiceStudioSettings{speed, instructions?}`. Validator từ chối tên field khớp `key\|token\|secret\|password` |
   | `LocalizationInput` | `aweme_id` (regex `^\d{1,32}$`), `channel_id` (regex `^[A-Za-z0-9_-]{1,128}$`), `generation`, `video_key`, `target_lang`, `voice` |
   | `Transcript` | Ghi lên S3. Payload chỉ mang `transcript_key`, `segment_count` |
   | `SubtitleTrack` | Payload mang `subtitle_key`, `region: BBox \| None` |
   | `LocalizedSegment` | `id`, `start_ms`, `end_ms`, `slot_ms`, `src_zh`, `text_vi`, `on_screen: bool` |
   | `LocalizedScript` | `aweme_id`, `version: int`, `prompt_version`, `segments[]`. Payload chỉ mang `script_key` và `version` |
   | `DubSegment` | `id`, `text_vi`, `tts_key`, `duration_ms`, `stretch_ratio` (mọi segment, trong khoảng [0.85, 1.15]), `overflow_unresolved: bool` |
   | `DubbedAudio` | `audio_key`, `audio_sha256`, `script_key`, `script_version`, `segments_key` |
   | `EditSpec` | `source_key`, `audio_key`, `audio_sha256`, `script_version`, `width`, `height`, `fps`, `duration_ms`, `cover \| null`, `captions[]`, `font` |
   | `ReviewVerdict` | `pass`, `scores{fidelity, timing, visual}`, `issues[{segment_id?, stage, reason}]` |

2. **Sinh schema.** `python -m localization.contracts.export` sinh ra schema. Test drift so với
   file đã lưu. Mutation drift nằm trong harness Python này.
3. **Phân loại lỗi.**

   | Lỗi | Phân loại |
   |---|---|
   | 408, 429, 5xx, timeout, lỗi kết nối | Transient |
   | 4xx khác, `no_audio_track`, `no_video_stream` | Permanent → `ApplicationError(non_retryable=True)` |
   | `NoSuchKey` | `ArtifactMissingError`, non-retryable, mang tên của stage đã sinh ra artifact (D10) |

4. **Provider.**
   - **`OpenAICompatStt`**: `verbose_json`, word và segment granularity.
   - **`OpenAICompatTts`**: `/v1/audio/speech`, với `model=TTS_MODEL` và `voice=voice_id`.
   - **`VisionOcr`**: gọi chat completions. `bbox.py` chuẩn hoá bbox theo `OCR_BBOX_FORMAT` về
     pixel `[x, y, w, h]`.
   - **`DemucsSeparator`**:
     - Gọi subprocess với argv dạng list, có `--` đứng trước đường dẫn.
     - Thư mục tạm tạo bằng `tempfile.mkdtemp()`, không suy ra từ id.
   - **`llm.model_for(role)`**: `OpenAIChatModel` với base URL lấy từ env.
   - **Validate `VOICESTUDIO_URL`** lúc load settings:
     - https thì luôn được.
     - http chỉ được khi host thuộc `VOICESTUDIO_ALLOW_PRIVATE_HTTP_CIDRS`.
     - Ngoài hai trường hợp đó thì từ chối khởi động.
5. **ObjectStore.** Hàm sinh key là nguồn duy nhất:

   | Hàm | Key |
   |---|---|
   | `work_key(aweme_id, round, name)` | `work/{aweme_id}/r{round}/{name}` |
   | `tts_cache_key(hash)` | `cache/tts/{hash}.wav` |
   | `render_key(aweme_id, spec_sha)` | `work/{aweme_id}/render-{spec_sha}.mp4` |
   | `review_key(aweme_id, generation)` | `review/{aweme_id}-g{n}.mp4` |
   | `output_key(aweme_id)` | `output/{aweme_id}.mp4` |

   Mọi `aweme_id` đều qua validator trước khi được dùng để sinh key.
6. **Codec guard (nguyên tắc 2).**
   - `SizeGuardCodec(PayloadCodec)`: khi encode, nếu `len(p.data) > 256 KiB` thì raise.
   - Gắn qua `DataConverter(payload_codec=...)`, kèm `PydanticAIPlugin()`, lúc `Client.connect`.
   - Test chạy qua **Client thật cộng worker** trong `WorkflowEnvironment`:
     - Activity trả 300KB thì workflow fail với lỗi của guard.
   - Mutation:
     - Bỏ codec → đỏ.
     - Gắn guard bằng một `PayloadConverter` tuỳ biến → đỏ, vì plugin âm thầm thay converter.
7. **Test kiến trúc (nguyên tắc 1, 5).**
   - `workflows/` chỉ được import:
     - `temporalio.workflow`
     - `localization.contracts`
     - `localization.text`
     - `localization.agents.temporal`, theo dạng passthrough mà pydantic-ai hướng dẫn
     - `datetime.timedelta`
     - Tên activity
   - Cấm `os`, `httpx`, `boto3`, `random`, `time`, `localization.providers`, `localization.storage`.
   - **Control:** import `boto3` gián tiếp qua `agents/` phải bị bắt.
   - `test_single_translation`: `synthesize`, `layout_captions` và `assemble_audio` không import `agents` hay `pydantic_ai`.
8. **Canary (nguyên tắc 3).**
   - Đặt mọi secret thành `canary-<uuid>`.
   - Chạy **class provider thật** gọi vào một stub HTTP local, với các kịch bản 200, 401, 500 và timeout.
   - Quét những nơi sau:
     - History, gồm `ActivityTaskFailed` và failure payload.
     - Span, qua `InMemorySpanExporter`.
     - stdout và stderr, qua `capfd`.
   - **Control:** cố ý log canary ra thì phải bị bắt.
   - **Mutation:** gán giá trị key đọc từ settings vào một field mới của `VoiceRef` → đỏ.
9. **Migration** `261026-localization-core.sql` trong schema `localization`. **Backup trước khi apply.**

   | Bảng | Cột |
   |---|---|
   | `glossary` | `channel_id`, `term_zh`, `term_vi` (tối đa 64 ký tự), `note` (tối đa 200 ký tự), `source_aweme_id`, `workflow_id`, `created_at`. PK `(channel_id, term_zh)` |
   | `voice_rates` | `voice_hash`, `syllables_per_sec`, `measured_at` |
   | `runs` | `aweme_id`, `generation`, `workflow_id`, `status`, `output_key`, `review_key`, `verdict jsonb`, `review_rounds`, `needs_human`, `updated_at`. PK `(aweme_id, generation)` |

   Role `localization` là owner của schema này.
10. **Refactor mutation runner.**
    - Định dạng `EXPECT` mới là `dir:phase:mut:ctl`. Ví dụ:
      - `downloader:07:28:5`
      - `localization:03:4:2`
      - `renderer:07:2:0`

      Chuyển các dòng cũ sang định dạng này.
    - `wanted()` và CLI nhận selector `localization/03`.
    - Fingerprint thêm các đuôi `py`, `tsx`, `json`, `ttf`, `sh`. `TRACKED_PATHS` thêm
      `services/localization`, `services/renderer/src`, `contracts`, `assets/fonts`.
    - `scripts/lib/mutation-protocol.sh` cung cấp `apply_mutation`, `expect_red`, `expect_green`
      và `restore`. Các hàm này in đúng những dòng output mà runner đang đếm.
    - **Control:** một harness cố ý không restore file `.py` thì guard D phải đỏ.

## Verification

- `cd services/localization && uv run pytest -q && uv run ruff check`
- `bash scripts/run-mutations.sh localization/03`: đỏ đủ 4 mutation, gồm thêm `import os`, bỏ
  codec, gắn guard như converter, và rò canary. 2 control xanh.
- `bash scripts/run-mutations.sh`: mọi dòng của downloader vẫn đúng số lượng sau khi đổi định dạng.
- `uv run pytest tests/providers -m live`: gọi VoiceStudio và OpenRouter thật. Chỉ chạy tay khi có env.

## Risk

Passthrough import của pydantic-ai trong sandbox phải làm đúng theo docs. Đã kiểm ở G1.

## Rollback

Service mới, không ai phụ thuộc. Runner có commit riêng, revert được.

## Tiến độ

Hoàn thành 2026-10-26. G2 (VoiceStudio) và G5 (bbox) vẫn bị bỏ qua theo quyết định ở phase 00, nên provider được kiểm bằng stub HTTP local đi qua đúng class thật.

Khác với plan:
- Harness `localization:03` có **11** mutation (plan nói 4): thêm drift của `EditSpec` (D8), hai kiểu vượt scanner, pre-check payload, 2xx/WAV và tiền tố `source/`. Hai control như plan. `EXPECT` ghi `localization:03:11:2`.
- Selector cũ (`04`, `infra`, `12`) vẫn chỉ chọn harness của downloader. Service khác dùng `dir/phase`, hoặc `dir/*` cho cả service. CI downloader chạy `downloader/*`, job `localization` mới chạy `localization/*`.
- `scripts/test-run-mutations.sh` kiểm các guard của chính runner trong một sandbox (gồm control "harness không restore file `.py/.tsx/.json/.ttf` thì guard D đỏ"). Chạy trong job `localization`.
- Fingerprint bỏ qua `.venv`, `node_modules`, `__pycache__` và cache của pytest/ruff, vì chúng tự đổi và không phải source.
- Allowlist import của `workflows/` đúng như plan (không thêm `temporalio.common`/`exceptions`); phase 04 cần thì thêm có chủ đích.
- Test `test_single_translation` bỏ qua từng stage chưa có file (`synthesize`, `layout_captions`, `assemble_audio`); control của nó vẫn chạy.
- Migration chỉ được áp lên database tạm trong test (`loc_migration_*`, bị xoá sau test). Chưa áp lên `reup`: cần `pg_dump` trước.

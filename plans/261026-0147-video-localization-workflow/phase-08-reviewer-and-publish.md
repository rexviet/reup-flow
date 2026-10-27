---
phase: 8
title: "Reviewer, vòng chạy lại, publish và nối end-to-end"
status: in-progress
priority: P1
effort: "1.5d"
dependencies: [6, 7]
---

# Phase 08: Reviewer, vòng chạy lại, publish

<!-- Updated: Red Team 2026-10-26 - Reviewer chạy trong một activity, cấm presigned URL, định tuyến dub chạy lại 4b, artifact theo vòng, review/ cho needs_human, AC2 so metadata TTS -->

## Goal

Phase này làm bốn việc:
- Nối nhánh âm thanh (phase 06) và nhánh hình ảnh (phase 07) thành pipeline đủ 9 bước.
- Bước 8 chấm điểm mp4 **bên trong một activity**.
- Chỉ chạy lại stage bị lỗi, và sub luôn khớp với lời đọc.
- Kết thúc bằng một trong hai: publish ra `output/`, hoặc `needs_human` với bản render lưu ở `review/`.

## Files to Create / Modify

- `src/localization/agents/reviewer.py`: agent pydantic-ai **thường**, không dùng `TemporalAgent`.
- `src/localization/activities/review_render.py`: activity chạy Reviewer, tool là các hàm Python bên trong activity đó.
- `src/localization/activities/publish.py`
- `src/localization/workflows/video_localization.py`, `src/localization/workflows/review_routing.py` (hàm thuần chọn stage chạy lại).
- `tests/workflows/test_review_loop.py`
- `tests/agents/test_reviewer.py`
- `tests/mutations/phase-08.sh` và dòng `EXPECT`.
- Ngoài danh sách gốc (xem "Khác với phase file"): `text/glyphs.py`, các activity `assemble_script`, `dub_script`, `script_document` (round), `contracts/models.py`, `agents/localizer.py`, `agents/dubber.py`, `agents/temporal.py`, `workers/run.py`, `docker-compose.yml`, test support và histories `phase-08`.

## Tasks & Steps

1. **`review_render` (D11).**
   - Chạy trên queue `llm`, timeout 10 phút, tối đa 2 lần thử.
   - Bên trong activity, Reviewer có 4 tool, đều là hàm Python cục bộ:

     | Tool | Mô tả |
     |---|---|
     | `alignment_report` | Đọc `dub-segments.json`, đo LUFS và true peak trên mp4 |
     | `sample_frames` | `ffmpeg` cắt frame, trả `BinaryContent`. Tối đa 8 frame mỗi lần, 3 lần mỗi review |
     | `residual_source_text` | Chạy `OcrProvider` trên frame, tìm cue chữ Hán nằm ngoài hoặc xuyên qua hộp che |
     | `read_script` | Đọc script |

   - Ảnh chỉ nằm trong bộ nhớ của activity, **không bao giờ đi vào payload Temporal**.
   - **Cấm presigned URL.**
   - Instrument với `include_binary_content=False`.
   - Activity chỉ trả `ReviewVerdict`, payload nhỏ.
   - Retry activity thì chạy lại toàn bộ review. Đánh đổi này chấp nhận được vì review ngắn.
   - Nội dung video được bọc trong khối `<untrusted_video_text>` giống phase 05.
2. **Định tuyến chạy lại.** Code thuần, mỗi vòng `r` dùng key `work/{id}/r{r}/`.

   | Stage lỗi | Chạy lại |
   |---|---|
   | `localize` | 3 → 4 → 5 → (6 ‖ 4b) → 7. Truyền `issues` cho Localizer làm feedback |
   | `dub` | 5 (chỉ các segment được nêu) → script mới → **(6 ‖ 4b)** → 7 |
   | `caption` | 4b → 7 |

   Spec mới mang `script_version` và `audio_sha256` mới, nên `spec_sha` đổi và render chạy thật.
   Spec không đổi thì render được bỏ qua đúng.

   Giới hạn `review_rounds ≤ 2`. Hết lượt thì `needs_human`. Cờ an toàn từ phase 05 cũng dẫn tới `needs_human`.
3. **`publish`.**
   - **Đạt:** `CopyObject` sang `output/{aweme_id}.mp4`, upsert `runs`, `status='published'`.
   - **`needs_human`:** `CopyObject` sang `review/{aweme_id}-g{n}.mp4` (không có lifecycle), upsert `runs`. **Không ghi `output/`.**
   - Idempotent theo `(aweme_id, generation)`.
4. **Replay.** Lưu history của các run sau phase này vào `tests/replay/histories/phase-08/`.

## Verification

Chạy `uv run pytest tests/workflows/test_review_loop.py tests/agents/test_reviewer.py`.

| Kịch bản | Kỳ vọng |
|---|---|
| Fail `dub` (segment 3), rồi pass | Bước 5 (chỉ segment 3), 6, 4b và 7 chạy lại. Bước 2a, 2b, 3 không chạy lại. Text sub trong spec mới **bằng** metadata `text-vi` của các object TTS trong `DubbedAudio` (AC2) |
| Fail `caption`, rồi pass | Chỉ 4b và 7 chạy lại |
| Fail cả 2 vòng | `review/` có object. `HeadObject output/...` trả 404 |
| Pass ngay | `output/` có object, `runs.status='published'` |
| Review với 8 frame thật | Không có payload nào trong history vượt 256KB. Không có chuỗi base64 ảnh (`iVBOR`, `/9j/`) nào trong history |

**Mutation:** `bash scripts/run-mutations.sh localization/08` đỏ với:
- Chạy lại toàn bộ khi lỗi `dub`.
- Lỗi `dub` mà không chạy 4b.
- Publish khi đang `needs_human`.

**Thủ công:** một video thật chạy end-to-end và ra `output/{aweme_id}.mp4` (mốc M4).

## Risk

Vision model đắt. Đã giới hạn số frame và số lần gọi tool.

## Rollback

Workflow dừng ở bước 7. Publish tay bằng `mc cp`, có ghi trong runbook.

## Tiến độ

**Trạng thái: in-progress.** Code, test tự động (Python và Node), replay và harness mutation đã đăng ký xong; test xanh. Chưa đóng phase vì (1) chưa chạy được một video thật (VoiceStudio không với tới, `.env` thiếu `VOICE_ID`, `STT_MODEL`, `TTS_MODEL`, `LOC_DATABASE_URL`), nên Reviewer chưa chạy với model vision thật, `publish` chưa chạy với MinIO và Postgres của stack, và mốc M4 (một video ra `output/{aweme_id}.mp4`) chưa đạt; (2) mutation chưa chạy (theo quyết định của người dùng, chạy một lần sau phase 10). Mọi thứ cần provider thật được đánh dấu "chưa kiểm" bên dưới.

**Đã làm và đã kiểm**
- **Đường đi từ lồng tiếng tới publish.** Sau `assemble_audio`, workflow chạy `layout_captions` (media) → `render` (queue `render`, worker Node) → `review_render` (llm) → `publish` (io), tất cả sau `workflow.patched("review-publish")`. Worker production đăng ký đủ: `layout_captions` đã có ở `worker-media`; `review_render` và `publish` thêm vào queue `llm` và `io` của `worker-agents`; `render` do `worker-render` phục vụ. Test `test_every_activity_the_workflow_schedules_is_served_on_its_queue_by_a_real_worker` lấy mọi cặp (activity, queue) mà workflow thật phát ra trong history `phase-08` rồi đòi một worker production đăng ký đúng cặp đó (riêng `render` được đối chiếu với `services/renderer/src/worker.ts`); `test_no_stand_in_is_registered_by_a_production_worker` kiểm không có activity nào của worker production đến từ `tests/`. Không có stand-in nào trong wiring production.
- **`review_render`** (`activities/review_render.py`, `agents/reviewer.py`): agent pydantic-ai thường chạy trong một activity (queue `llm`, timeout 10 phút, tối đa 2 lần thử, heartbeat). Bốn tool: `read_script`, `alignment_report` (đọc `dub-segments.json`, đo LUFS và true peak trên mp4 bằng `loudnorm`), `sample_frames` (`ffmpeg` cắt JPEG, tối đa 8 frame mỗi lần và 3 lần mỗi review, trả `BinaryContent` trong `ToolReturn`), `residual_source_text` (hỏi `OcrProvider.detect_region` trên frame ở đúng kích thước video, so vùng chữ tìm được với hộp che). Chữ từ video vào qua khối `<untrusted_video_text>`. Ảnh chỉ nằm trong bộ nhớ của activity; instrument với `include_binary_content=False`; activity chỉ trả `ReviewVerdict`. Test: 8 frame JPEG thật cắt bằng `ffmpeg` từ một mp4 thật; history của một run với review 8 frame thật không có payload nào lớn hơn 256 KB và không có `iVBOR` hay `/9j/` sau khi giải mã từng payload (có control: ảnh có thật trong request của model, và hàm giải mã thấy được ảnh nằm trong payload); span không chứa byte ảnh (có control: cài đặt mặc định của pydantic-ai thì có); thư mục tạm bị xoá cả khi review lỗi; reviewer không dừng thì lỗi vĩnh viễn `ReviewRunaway`.
- **Định tuyến chạy lại** (`workflows/review_routing.py`, hàm thuần): chạy lại stage sớm nhất có vấn đề và không gì trước nó. `localize` → Localizer với ghi chú của reviewer (trong prompt, khối untrusted, không trong `deps`) → script phiên bản kế tiếp ở `r{n}/` → toàn bộ lồng tiếng của vòng đó (clip đã đọc là cache hit) → 4b → 7. `dub` → Dubber chỉ cho các segment được nêu (cùng ghi chú) → script mới → TTS cho câu mới → `commit_dub` giữ quyết định cũ của các dòng khác → `assemble_audio` → 4b → 7. `captions` → 4b → 7. Artifact của mỗi vòng nằm ở `work/{id}/r{n}/` (script, `dub-segments.json`, `dubbed.wav`, `edit-spec.json`); bản đầu không bị ghi đè. Test với workflow thật: lỗi `dub` ở segment 1 thì chỉ Dubber cho segment 1 nói (không dòng nào khác), ingest, transcribe, detect_subtitles và Localizer không chạy lại, layout vòng 1 dùng script v3 và track `r1/dubbed.wav`, `output/` có bản render thứ hai; lỗi `localize` thì Localizer chạy lại (prompt có ghi chú trong khối untrusted), script v3, không stage chuẩn bị nào chạy lại; lỗi `captions` thì chỉ 4b và 7; spec đã render thì render không chạy lại (đếm theo hash).
- **Giới hạn vòng**: tối đa 2 lần review (một lần chạy lại), lần thứ ba không bao giờ có.
- **`publish`** (`activities/publish.py`): `CopyObject` phía server, không byte nào đi qua tiến trình. `published` → `output/{aweme_id}.mp4`; `needs_human` → `review/{aweme_id}-g{generation}.mp4`, không bao giờ ghi `output/`; `failed` → không copy gì. Cả ba upsert một dòng `localization.runs` theo `(aweme_id, generation)` (gọi hai lần ra cùng trạng thái; kiểm trên Postgres thật với migration thật, trong database tạm). Activity tự từ chối (`PublishRefused`, không retry) ghi `output/` cho bất cứ thứ gì không phải một render đã pass review mà không có gì bị cờ, bất kể workflow gọi gì.
- **AC2 (một bản dịch)**: layout và Reviewer luôn đọc script mà track được dựng từ đó (`DubbedAudio.script_key`, phiên bản của Dubber). Test `test_after_a_rerun_the_captions_are_the_text_that_was_spoken` chạy workflow thật với activity thật cho lồng tiếng (cache TTS, Dubber viết lại, ghi script), `assemble_script` và `layout_captions` trên cùng một object store: ở cả vòng 0 và vòng 1 (sau khi Dubber viết lại dòng 1), mỗi caption trong `edit-spec.json` bằng `text-vi` trong metadata của object TTS mà segment đó phát; spec vòng 1 hiện câu mới, không phải câu cũ. `check_one_translation` của layout (phase 07) vẫn từ chối nếu text sub khác text đã đọc.
- **Replay**: histories `phase-08` (7 kịch bản: pass ngay, Dubber chạy lại cho một dòng, Localizer chạy lại với ghi chú, layout chạy lại, cần người sau hai review, bản dịch bị cờ, layout fail vĩnh viễn) ghi bằng workflow thật; `phase-04/05/06` vẫn replay (không ghi lại); control: cùng các bước thêm vào mà không có marker làm `phase-06` đỏ; có test chứng minh histories cũ thật sự không có các bước mới.
- **Chạy lại có `workflow.patched`; đối số activity luôn đầy đủ.** Một activity có tham số mặc định mà bị gọi với ít đối số hơn số tham số thì SDK không chuyển kiểu cho đối số nào (nhận `dict`): workflow luôn gửi đủ (`assemble_script` thêm `round` và `version`, `write_dubbed_script` và `commit_dub` thêm `round`). Replay không so đối số nên histories cũ vẫn xanh. Có test ghim hành vi này của SDK.
- **Từ chối ký tự không hiển thị được trước khi trả tiền TTS** (`text/glyphs.py`, hàm thuần không cần file font): chữ cái ngoài Latin (Hán, Nhật, Hàn, Kirin...), emoji và ký hiệu hình, mũi tên, dấu toán không đọc được, dấu câu toàn chiều rộng và CJK. Validator của Localizer trả `ModelRetry` nêu dòng và ký tự; câu viết lại của Dubber chứa ký tự đó bị từ chối như câu có liên lạc lạ (dòng giữ chữ cũ). Kiểm tra theo font ở layout (`UncoveredGlyphs`) **không đổi** và vẫn là chốt cuối cho ký tự hiếm mà bộ lọc này cho qua.
- Agent bỏ cuộc (validator không thoả sau số lần thử, vượt giới hạn dùng) trong `run_localizer` và `run_dubber` trở thành `ApplicationError` không retry (`UnexpectedModelBehavior`, `UsageLimitExceeded`) để workflow bắt được và ghi lý do lên dòng `runs`; trước đó nó chỉ làm workflow fail mà không để lại dấu vết trên run.
- Cấu hình: `REVIEWER_MODEL` thêm vào compose (`worker-agents`) và allowlist của `compose-constraints.spec.ts`; worker-agents từ chối khởi động nếu thiếu (model vision riêng, không rơi về model của Localizer vì model chữ có thể không đọc ảnh). `/tmp` của `worker-agents` tăng 512m lên 1g (Reviewer giữ bản sao video để cắt frame).
- Mutation: **đã đăng ký, chưa chạy**: harness `localization:08` có 48 mutation và 2 control (`EXPECT`: `localization:08:48:2`), và `renderer:07` đã thêm 10 mutation cho phần sửa của phase 07 (`renderer:07:65:5`). Chưa chạy `scripts/run-mutations.sh`. Mẫu thay thế của harness 08 được kiểm là khớp đúng một lần trong file nguồn và mọi tên test được đòi đỏ đều tồn tại, bằng một script đọc (không chạy test nào); việc chúng thật sự đỏ chưa được chứng minh.
- `uv run pytest -m "not live"`: 926 passed, 0 skipped (6 live loại); `uv run ruff check src tests` và `ruff format --check` sạch. `pnpm test` của renderer: 93 passed; `pnpm lint` và `tsc --noEmit` sạch. `compose-constraints.spec.ts`: 25 passed.

**Quyết định cần người dùng xác nhận** (spec im lặng, đã chọn mặc định an toàn nhất)
1. **Run bị Localizer cờ `needs_human`, hoặc có câu Dubber viết lại bị từ chối: dừng trước publish, giữ bản render ở `review/{aweme_id}-g{n}.mp4`, ghi `runs.status = 'needs_human'`, không bao giờ ghi `output/`.** Giữ nguyên chi phí lồng tiếng đã trả (Dubber chạy trước khi cờ được đọc ở phase 06) và vẫn dựng một lần để người xem có cái để xem; Reviewer chấm một lần để người xem có điểm và ghi chú trong tay, nhưng run không bị gửi vòng lại (một run bị cờ không tự sửa bằng cách chạy lại). Phương án khác: dừng ngay sau Localizer khi có cờ, đỡ tốn TTS và render nhưng người xem không có video. Lý do của dòng bị từ chối: Dubber đọc text không tin cậy, một câu viết lại bị từ chối là dấu hiệu injection hoặc ký tự không hiển thị được.
2. **Lỗi vĩnh viễn của layout và renderer** (`UncoveredGlyphs`, `ImplausibleCover`, `VideoOutOfBounds`, `InvalidEditSpec`, `ScriptDubMismatch`, `FontMismatch`, `AudioHashMismatch`, `RenderDeadline`, và mọi lỗi activity khác sau lồng tiếng): **run fail, không retry vô hạn, lý do ghi lên dòng `runs` (`status = 'failed'`, `verdict.reasons = ["<Loại>: <thông điệp>"]`), không có gì ở `output/` hay `review/`.** Không tự định tuyến về Localizer (mỗi vòng tốn thêm LLM và TTS và có thể lặp); người vận hành đọc lý do rồi tăng `generation` để chạy lại. Phương án khác: `UncoveredGlyphs` → một vòng Localizer kèm danh sách ký tự; `ImplausibleCover` → `needs_human`. Giảm chi phí: ký tự không hiển thị được bị từ chối **trước** TTS (mục trên), nên `UncoveredGlyphs` chỉ còn xảy ra với ký tự hiếm mà bộ lọc cho qua.
3. **Hai review, một lần chạy lại.** "`review_rounds ≤ 2`" được hiểu là hai lần review. Review thứ hai fail hoặc vấn đề thuộc stage không sửa được bằng chạy lại ở đây (`render`, `transcribe`, `detect_subtitles`, một vấn đề `dub` không nêu segment) → `needs_human` ngay.
4. **Layout chạy lại mà ra cùng spec thì không review lần hai** (`needs_human` với lý do "the rerun produced the same video"). Layout là hàm thuần của script và track, nên lỗi `captions` chạy lại 4b trên cùng đầu vào cho cùng video; review lại chỉ lặp lại kết quả. Hệ quả: đường `captions` gần như chỉ có tác dụng khi layout đổi giữa hai vòng.

**Khác với phase file**
- Reviewer: `residual_source_text` dùng `OcrProvider.detect_region` (bộ dò vùng chữ, không phải bộ đọc chữ): nó cho biết có vùng chữ nằm ngoài hộp che hay không, không cho biết chữ gì. Phần "cue chữ Hán" của phase file là heuristic theo vị trí.
- Các tham số `round` và `version` của `assemble_script`, `round` của `write_dubbed_script` và `commit_dub`; `write_dubbed_script` trả `DubbedScriptRef` (thêm `refused_ids`) để lý do `needs_human` không mất; `LocalizationResult.publish` mới (mặc định rỗng, histories cũ vẫn parse).
- `ghi review/{aweme_id}-g{n}.mp4`: dùng `spec.generation` của run.
- `tests/architecture/import_scanner.py`: thêm `localization.workflows.review_routing` vào allowlist import của workflow (module thuần; quét vẫn đọc mọi import của nó).
- Phần sửa phase 07 chạy trước bằng commit riêng (xem phase 07).

**Chưa kiểm được (nói thẳng)**
1. **Không có video thật nào chạy hết pipeline.** Chưa có Reviewer với model vision thật (chưa biết nó có đưa ra issue có ích, có gọi tool đúng, có tốn bao nhiêu), chưa có OCR thật trên frame đã render, chưa có `publish` với policy MinIO của key `localization` (CopyObject từ `work/` sang `output/` và `review/`), chưa có dòng `runs` trên database `reup` (migration `261026-localization-core.sql` chưa áp lên `reup`; test dùng database tạm).
2. AC3 (mỗi `spec_sha256` chỉ `renderMedia` một lần, đếm bằng counter phía renderer, qua vòng review) chưa đo với renderer thật: test dùng render giả đếm theo hash và renderer có test riêng cho single-flight và key theo hash.
3. Chưa chạy `worker-agents`, `worker-media`, `worker-render` trong stack compose đầy đủ với workflow này.
4. Mutation (48 + 2 + 10) chưa chạy.

**Giới hạn đã biết**
- Review dùng tối đa 24 tool call và 12 request mỗi lần; lỗi vượt giới hạn là `ReviewRunaway`, không retry.
- Một artifact của vòng sau (`r1/` trở đi) bị mất (lifecycle 30 ngày, hiếm trong vài phút) thì run fail với `ArtifactMissingError`, không có cơ chế chạy lại stage sinh ra nó như vòng đầu (D10 chỉ áp cho vòng đầu).
- `LocalizerDeps` và `DubberDeps` không mang ghi chú của reviewer (deps chỉ mang key và số): ghi chú đi trong prompt, nên nằm trong payload của request model (đúng như mọi hội thoại), không trong payload của tool.
- Ghi chú của reviewer là text do model viết về video; nó vào prompt của Localizer và Dubber trong khối `<untrusted_video_text>` nhưng một model vẫn có thể bị ảnh hưởng. Chưa có đo lường.
- Dubber đọc lại các dòng đã vừa slot khi reviewer nêu tên chúng; kết quả có thể làm dòng đó dài hơn slot (khi đó `overflow_unresolved` và vòng review hai thấy).
- Review thứ hai chạy cả khi chỉ có 4b đổi: chi phí vision nhân đôi cho run phải chạy lại.
- `Dubber` `PROMPT_VERSION` giữ `dubber-1` dù hướng dẫn có thêm đoạn về ghi chú của reviewer.
- `review/` và `output/` không có lifecycle; `output/{aweme_id}.mp4` bị ghi đè khi cùng video publish lần nữa ở generation khác (theo thiết kế: một video một đầu ra).
- Hai replica `worker-agents` không ảnh hưởng tính đúng của `publish` (idempotent), nhưng hai run của cùng `aweme_id` khác generation cùng ghi `work/{id}/r0/...` (từ phase 04); chạy lại một video khi run cũ còn sống có thể ghi chồng artifact.

## Chạy một video thật

Điều kiện: VoiceStudio với tới được, và `.env` có (chỉ cần có mặt, không in giá trị): `POSTGRES_USER`, `POSTGRES_PASSWORD`, `LOC_DATABASE_URL` (role `localization`), `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, `S3_*`, `LOC_S3_ACCESS_KEY_ID`, `LOC_S3_SECRET_ACCESS_KEY`, `RENDER_S3_ACCESS_KEY_ID`, `RENDER_S3_SECRET_ACCESS_KEY`, `OPENROUTER_API_KEY`, `LOCALIZER_MODEL`, `DUBBER_MODEL` (có thể bỏ trống: dùng model của Localizer), **`REVIEWER_MODEL` (model đọc được ảnh, bắt buộc)**, `OCR_MODEL` và `OCR_BBOX_FORMAT` (phase 04 cũng cần), `VOICESTUDIO_URL`, `VOICESTUDIO_API_KEY` (nếu server bật `OMNIVOICE_API_KEY`), `STT_MODEL`, `TTS_MODEL`, `VOICE_ID` (phải có trong `GET /v1/audio/voices`; có giọng tiếng Việt), `LANGFUSE_*` (để có trace; không bắt buộc cho kết quả).

```bash
cd /Users/vietphung/Data/reup-flow

# 1. Backup trước khi đổi schema hay dữ liệu (cần người dùng đồng ý áp migration lên `reup`).
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
  > backups/reup-$(date +%Y%m%d-%H%M%S).dump

# 2. Migration, theo thứ tự (bỏ qua cái đã áp; mỗi file chạy một transaction, dừng khi lỗi).
#    Trạng thái lúc viết: database `reup` chưa có `localization_outbox` và chưa có schema
#    `localization`. Bảng outbox phải có TRƯỚC khi chạy downloader mới (`markDone` ghi vào nó).
for f in services/downloader/migrations/261026-localization-outbox.sql \
         services/localization/migrations/261026-localization-core.sql \
         services/localization/migrations/261026-outbox-starter-grants.sql; do
  docker compose exec -T postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -1' < "$f"
done

# 3. Video phải là `done` với storage_path dạng s3://reup-media/source/... (phase 01).
#    Video đang ở đĩa local: dry-run rồi chạy `scripts/migrate-local-storage-to-s3.ts` theo
#    chú thích đầu file (cần dừng lane và có backup). Kiểm:
docker compose exec -T postgres sh -c \
  "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \
   \"SELECT aweme_id, status, storage_path FROM videos WHERE aweme_id = '<AWEME_ID>'\""

# 4. Dựng stack (dùng lại stack đang chạy; chỉ build và khởi động phần của localization).
docker compose up -d --build db-init minio-init temporal temporal-namespace-init \
  worker-agents worker-media worker-render localization-starter
docker compose ps                       # worker-*, localization-starter: running
docker compose logs --tail 20 worker-agents worker-media worker-render
#   worker-agents: "worker agents serving queues ['workflows', 'llm', 'io', 'stt', 'tts']"
#   worker-media:  "worker media serving queues ['media']"; worker-render: "render worker ready"
# Một worker thiếu cấu hình (REVIEWER_MODEL, LOC_DATABASE_URL, ...) tự thoát ngay lúc khởi động
# kèm tên biến thiếu.

# 5. Cho một video vào hàng đợi: một dòng outbox (nếu video vừa tải xong bằng downloader chạy
#    với STORAGE_BACKEND=s3 thì `markDone` đã ghi dòng này, bỏ qua bước này).
docker compose exec -T postgres sh -c \
  "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \
   \"INSERT INTO localization_outbox (aweme_id) SELECT aweme_id FROM videos \
     WHERE aweme_id = '<AWEME_ID>' AND status = 'done' AND storage_path LIKE 's3://%' \
     ON CONFLICT DO NOTHING\""
#    Chạy lại một video đã có dòng (phải tăng generation, workflow id có hậu tố -g{n}):
#    UPDATE localization_outbox SET generation = generation + 1, state = 'pending',
#      dispatched_at = NULL, next_attempt_at = now(), dispatch_attempts = 0
#    WHERE aweme_id = '<AWEME_ID>';

# 6. Theo dõi. Starter bắt dòng trong vài giây và start workflow `video-<AWEME_ID>-g<generation>`.
docker compose logs -f localization-starter worker-agents worker-media worker-render
#    Temporal UI: http://127.0.0.1:8233 (namespace default, workflow id ở trên).

# 7. Kết quả.
docker compose exec -T postgres sh -c \
  "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -x -c \
   \"SELECT status, output_key, review_key, needs_human, review_rounds, verdict \
     FROM localization.runs WHERE aweme_id = '<AWEME_ID>'\""
docker compose run --rm --no-deps --entrypoint sh minio-init -c \
  'mc alias set l http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null \
   && mc ls l/reup-media/output/ l/reup-media/review/'
#    published     -> reup-media/output/<AWEME_ID>.mp4, runs.status = 'published'
#    needs_human   -> reup-media/review/<AWEME_ID>-g<n>.mp4, lý do ở runs.verdict->'reasons'
#    failed        -> không có file, lý do ở runs.verdict->'reasons'
#    Lấy file ra xem:  mc cp l/reup-media/output/<AWEME_ID>.mp4 /tmp/   (trong cùng container
#    run, hoặc qua console MinIO http://127.0.0.1:9001).
```

Dọn sau khi chạy: `docker compose stop worker-agents worker-media worker-render localization-starter` nếu không cần chạy tiếp (không để worker mồ côi).

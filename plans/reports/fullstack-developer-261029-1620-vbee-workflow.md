# Workflow lồng tiếng bằng Vbee (`VbeeDubbingWorkflow`) và phần nối dây

Ngày 2026-10-29. Kế hoạch: `plans/261029-1544-vbee-dubbing-pipeline/plan.md`, phase 4.

## Kết quả

- Có workflow type mới `VbeeDubbingWorkflow` (`services/localization/src/localization/workflows/vbee_dubbing.py`). Workflow này lồng tiếng một video chỉ bằng Vbee, không cần Colab hay VoiceStudio.
- `VideoLocalizationWorkflow` không bị sửa dòng nào. Mọi bộ history cũ vẫn replay được (262 test replay đều xanh, gồm cả bộ mới).
- Worker `agents` phục vụ cả hai workflow. Starter chọn workflow theo `LOCALIZATION_PIPELINE`, mặc định là `voicestudio`, nên chưa có gì thay đổi cho đến khi người dùng đổi `.env`.
- Không có commit, không rebuild hay restart docker, không chạy video thật, không gọi Vbee hay LLM thật.

## Luồng và queue của từng activity

| # | Bước | Activity | Queue |
|---|---|---|---|
| 1 | Tách giọng, dò phụ đề gốc, đo tốc độ giọng, chạy song song | `ingest`, `detect_subtitles`, `voice_rate` | `media`, `io`, `tts` |
| 2 | Vbee STT (bắt đầu ngay khi `ingest` xong) | `vbee_stt` (mới) | `stt` |
| 3 | Chia slot | `plan_slots` | `io` |
| 4 | Rút gọn các dòng quá dài, theo tốc độ đo trên câu mẫu | `agent__slot_shortener__model_request` (+ `_repair_`) | `llm` |
| 5 | Ghi script | `write_slot_script` (mới) | `io` |
| 6 | TTS, chia đều batch | `plan_synthesis`, `synthesize_segments` | `io`, `tts` |
| 7 | Hiệu chỉnh tốc độ: đo trên clip thật, rút gọn lại một lần các dòng không vừa, ghi script mới, đọc lại | `clip_durations` (mới), shortener, `write_slot_script`, synthesis | `io`, `llm`, `tts` |
| 8 | Ghi dub-segments, mix | `commit_dub`, `assemble_audio` | `io`, `media` |
| 9 | Dàn phụ đề, review bằng code | `layout_captions`, `review_dub` (mới) | `media`, `io` |
| 10 | Nếu review thấy dòng bị tràn: rút gọn đúng các dòng đó một lần, đọc lại, mix, dàn và review lại. Tối đa 2 lượt review như workflow cũ | `clip_durations`, shortener, `write_slot_script`, synthesis, `commit_dub`, `assemble_audio`, `layout_captions`, `review_dub` | như trên |
| 11 | Render một lần (layout cuối), publish | `render`, `publish` | `render`, `io` |

Khi có lỗi, `_record_failure` ghi run thành `failed` kèm lý do, giống workflow cũ. Kết quả trả về là `VbeeDubbingResult` (ingest, subtitles, slots, script, dub, publish).

**Lý do chọn queue:**
- `vbee_stt` chạy trên `stt`, không chạy trên `io`.
  - Job STT được poll vài chục giây đến vài phút. Nếu đặt trên `io` (`IO_CONCURRENCY=2`), nó sẽ giữ một trong hai slot và làm nghẽn các activity ngắn của mọi run (publish, plan_synthesis...).
  - Giới hạn của `stt` (`STT_CONCURRENCY`) đúng nghĩa "số job STT trả phí chạy cùng lúc".
  - Ở chế độ `vbee`, `transcribe` của VoiceStudio không chạy, nên không có tranh chấp.
  - Timeout là 70 phút: workflow không đọc được settings, nên lấy mức trần 3600 s của `VBEE_POLL_TIMEOUT_SECONDS` cộng thời gian upload. Heartbeat 30 s (activity heartbeat 5 s/lần) phát hiện worker chết sớm hơn nhiều. Retry tối đa 4 lần, vì mỗi lần là một job bị tính tiền.
- `plan_slots` và `review_dub` chạy trên `io`, vì chúng chỉ là một lượt ffmpeg vài giây. Đặt trên `media` thì chúng sẽ phải chờ sau Demucs của video khác. Cả hai có `heartbeat_timeout` 60 s.

## Các quyết định

**Voice id nằm ở đâu.**
- Input của run chỉ mang marker không bí mật `VoiceRef(provider="vbee", voice_id="vbee-configured-voice", settings=VbeeSettings())`.
- `providers/tts_router.ConfiguredVoices.resolve` thay marker bằng `settings.vbee_voice_id` bên trong activity. Phép thay dùng `model_copy`, không validate, để một giá trị bị từ chối không bị in ra trong lỗi validation.
- Resolver được truyền vào `Synthesizer`, nên dùng cho cả `plan_synthesis`, `synthesize_segments`, `commit_dub` và `clip_durations`. `VoiceRateActivity` cũng nhận resolver này.
- Cache key TTS là `tts_hash` tính trên voice đã resolve. Vì vậy đổi voice cấu hình sẽ đổi mọi hash, nhưng chỉ có hash rời khỏi activity.
- Một VoiceRef `vbee` mang id riêng (không phải marker) bị từ chối với `PermanentProviderError`, và lỗi không in id.
- Voice của VoiceStudio đi qua nguyên vẹn, nên hash của pipeline cũ không đổi (có test).

**Provider được chọn thế nào.**
- `TtsByProvider` chọn provider theo `VoiceRef.provider`: `vbee` đi `VbeeTts`, còn lại đi provider cũ.
- Pipeline mới dùng chung queue `tts`, chung `Synthesizer`, chung cache clip, chung cách chia piece và heartbeat. Không cần queue mới, và workflow cũ không đổi gì.
- `engine` của VoiceStudio bị bỏ qua cho voice Vbee, trong `voice_rate` và trong workflow mới (truyền `None`).

**`src_zh` chứa gì.**
- Chuỗi rỗng `""`, hợp lệ với `Text`. Không có câu tiếng Trung, mà Vbee nghe tiếng Trung rồi trả tiếng Việt.
- Với nguồn rỗng, `line_findings` chỉ còn bắt dòng rỗng hoặc dòng có chữ không phải tiếng Việt. Không có số hay tên nào bị coi là "mất so với nguồn"; việc này shortener đã tự kiểm so với câu Vbee nghe được.
- Tôi không đưa câu tiếng Việt gốc vào `src_zh`, vì regex Latin của `line_findings` sẽ bắt các âm tiết không dấu ("ba", "tu") và báo sai mỗi khi rút gọn. Phase 3 đã ghi nhận đúng vấn đề này.
- Bước kiểm an toàn được giữ: `write_slot_script` so dòng đã rút gọn với dòng Vbee nghe được. Nếu xuất hiện liên hệ mới (URL, domain, số điện thoại), run thành `needs_human` với `safety_flag`.

**Video không có lời.**
- Ở workflow cũ, transcript rỗng vẫn đi hết đường: script rỗng, không đọc dòng nào, track chỉ có nhạc, không có phụ đề, review rồi render, rồi publish.
- Workflow mới làm y như vậy với `has_speech=False`: không rút gọn, không TTS, không đo tốc độ, `commit_dub` rỗng, track chỉ có nhạc, rồi layout, review, render, publish.

**Hiệu chỉnh tốc độ.**
1. Lần rút gọn đầu dùng tốc độ của `voice_rate`, đo trên câu mẫu.
2. Sau TTS, `clip_durations` trả độ dài thật của từng clip. Tốc độ đo được là tổng âm tiết chia tổng thời gian của các clip.
3. Các slot không vừa ở tốc độ đó (`overflowing`) được rút gọn lại một lần ở chính tốc độ đó, rồi ghi script mới và chỉ đọc lại các dòng mới; các dòng khác là cache hit.
4. Nếu giọng đọc nhanh hơn câu mẫu (6 so với 5,4 như spike đo), thường không có gì phải làm thêm.

**Rút gọn sau review.**
- Các id có issue stage `dub` được rút gọn bằng tùy chọn mới `ids` của `slot_shortener.shorten` và `run_slot_shortener`. Với `ids`, đúng các dòng đó được hỏi, kể cả khi ước lượng của chúng vẫn vừa.
- Tốc độ dùng là tốc độ thật chậm nhất trong các dòng đó (âm tiết chia độ dài clip, chỉ xét dòng có clip dài hơn slot). Ở tốc độ này, ước lượng của mỗi dòng bằng đúng thời gian nó đã được đọc.
- Nếu không còn dòng nào rút gọn được thì vòng lặp dừng và run đi `needs_human` kèm lý do.

**Timeout TTS.**
- Mỗi batch được 2 phút cộng 20 s cho mỗi piece của các dòng trong batch (khoảng 4 s poll mỗi piece, nhân 5 lần biên). Batch nào quá hạn sẽ được retry, và các clip đã làm xong là cache hit.
- Số piece tính bằng `split_line`. Phần cắt câu thuần (không I/O) được chuyển nguyên văn từ `activities/tts_pieces.py` sang `text/pieces.py`, để workflow import được mà không vi phạm `tests/architecture/test_workflow_imports.py`. `tts_pieces` re-export lại các tên cũ, nên synthesis và test cũ không đổi.

**Import từ workflow cũ.**
- Workflow mới dùng lại queue, retry policy, các dict cấu hình activity và các helper thuần (`_retry`, `_describe`, `_failed`, `tts_batches`) bằng cách import từ `workflows/video_localization.py`. File đó không bị sửa.
- Để làm được vậy, tôi thêm `localization.workflows.video_localization` vào allowlist của `tests/architecture/import_scanner.py`. Module này nằm trong `workflows/`, nên vẫn bị scanner quét theo cùng luật.

## Settings và các dòng `.env` để chuyển sang Vbee

Setting mới: `LOCALIZATION_PIPELINE` (`voicestudio` | `vbee`, mặc định `voicestudio`). Setting này đã có trong `.env.example` và compose.
- Ở chế độ `vbee`:
  - Starter không gọi VoiceStudio (không gọi `/v1/audio/voices`) và không gọi Vbee. Nó chỉ kiểm có đủ `VBEE_APP_ID`, `VBEE_API_TOKEN`, `VBEE_VOICE_ID`; nếu thiếu thì trả `CONFIG_ERROR` và không đụng vào row.
  - Worker `agents` từ chối khởi động nếu thiếu ba setting đó, và không cần `VOICESTUDIO_URL`, `STT_MODEL`, `TTS_MODEL`.
- Workflow id vẫn là `video-<aweme>-g<generation>`.
- Giới hạn `MAX_OPEN_WORKFLOWS` tính chung cho run của cả hai workflow type.

Các dòng người dùng cần thêm hoặc sửa trong `.env`:

```
LOCALIZATION_PIPELINE=vbee
VBEE_APP_ID=<app id>
VBEE_API_TOKEN=<token>
VBEE_VOICE_ID=<id giọng clone>
```

`VBEE_BASE_URL` (mặc định `https://api.vbee.vn`) và `VBEE_POLL_TIMEOUT_SECONDS` (mặc định 300) giữ nguyên. `OPENROUTER_API_KEY`, `LOCALIZER_MODEL`, `OCR_MODEL`, `OCR_BBOX_FORMAT`, `LOC_DATABASE_URL` vẫn cần như trước (shortener dùng model của Localizer, còn dò phụ đề dùng OCR). Sau khi sửa `.env`, cần recreate `localization-starter` và `worker-agents`.

Compose:
- `worker-agents` nhận thêm `LOCALIZATION_PIPELINE` và `VBEE_*` (5 biến).
- `localization-starter` nhận thêm `LOCALIZATION_PIPELINE`, `VBEE_APP_ID`, `VBEE_API_TOKEN`, `VBEE_VOICE_ID`.
- `worker-media` không nhận gì thêm.

## File đã đổi

Thêm mới:
- `src/localization/text/pieces.py` (chuyển nguyên văn từ `tts_pieces`)
- `src/localization/workflows/vbee_dubbing.py`
- `src/localization/activities/vbee_stt.py`
- `src/localization/activities/slot_script.py`
- `src/localization/providers/tts_router.py`

Sửa:
- `contracts/vbee_flow.py`: marker, `ClipDuration(s)`, `VbeeDubbingResult`.
- `activities/synthesize.py`: tham số `resolve`.
- `activities/voice_rate.py`: `resolve`; không dùng engine cho voice Vbee.
- `activities/dub_script.py`: activity `clip_durations`.
- `activities/review_render.py`: tách `_checks` ra khỏi `_judge` (giữ nguyên thứ tự kiểm, nên hành vi review cũ không đổi), thêm activity `review_dub` và hàm `_verdict`.
- `activities/tts_pieces.py`: import lại từ `text.pieces`.
- `agents/slot_shortener.py`: `to_ask`, tham số `ids`.
- `agents/temporal.py`: `run_slot_shortener(..., ids)`.
- `providers/registry.py`: `vbee_stt`, `vbee_tts`; kiểm khi khởi động.
- `config.py`: `localization_pipeline`.
- `workers/run.py`: wiring.
- `starter/outbox_starter.py`.
- `.env.example`, `docker-compose.yml`, `services/downloader/test/compose-constraints.spec.ts`.

Test:
- Mới: `tests/support/vbee_fakes.py`, `tests/replay/vbee_controls.py`, `tests/replay/vbee_histories/*.json` (7 history).
- Sửa: `tests/architecture/import_scanner.py` (allowlist); `tests/replay/record_histories.py` (thêm bộ `vbee-dubbing`); `test_queue_concurrency.py` (queue `workflows` giờ có 2 workflow); `test_agent_wiring.py`, `test_prep_stage.py` (stand-in provider có thêm `vbee_stt` và `vbee_tts`).

## Test

Tổng cộng 93 test Python mới.

| File | Số test | Nội dung |
|---|---|---|
| `tests/workflows/test_vbee_dubbing.py` | 13 | Happy path đúng thứ tự lệnh và đúng queue; hiệu chỉnh tốc độ (mẫu 6, thật 4,5: chỉ slot 1 được hỏi với budget 18, chỉ dòng mới được đọc lại, script v2); giọng nhanh hơn mẫu thì không rút gọn thêm; review báo tràn thì chỉ slot đó được rút gọn với budget theo tốc độ thật, đọc lại ở r1, review lần 2 qua, render 1 lần; còn tràn sau 1 lần chạy lại thì `needs_human`; shortener không rút được thì dừng kèm lý do và `unfit_ids`; không có lời; Vbee STT lỗi vĩnh viễn (1 lần gọi, không retry, run `failed` với lý do); render lỗi; canary voice id (2 biến thể) có trong mọi lời gọi TTS nhưng không có trong history JSON và không có trong file nào của store; số học của timeout và tốc độ |
| `tests/workers/test_vbee_wiring.py` | 23 | Mỗi activity của workflow được phục vụ trên đúng queue workflow gửi tới (15, lấy từ config của workflow); agent shortener có trong `__pydantic_ai_agents__` và được phục vụ trên `llm`; queue `workflows` có cả hai workflow; activity TTS và STT nhận đúng router, resolver và `VbeeStt`; pipeline `vbee` dựng được Vbee mà không cần VoiceStudio, và thiếu một trong 3 setting thì `ConfigError` |
| `tests/replay/test_vbee_replay.py` | 21 | 7 history replay được; có đủ history và đúng workflow type; mỗi history đi đúng nhánh mang tên nó; 2 control (đổi thứ tự lúc bắt đầu, bỏ bước hiệu chỉnh tốc độ) đều bị `NondeterminismError` |
| `tests/providers/test_tts_router.py` | 11 | Marker thành voice cấu hình; id riêng bị từ chối mà không lộ id; worker thiếu voice; route theo provider; hash plan và metadata clip không chứa canary; đổi voice thì đổi hash; hash của voice VoiceStudio không đổi; `voice_rate` của voice Vbee đo bằng giọng cấu hình, không dùng engine |
| `tests/activities/test_slot_script.py` | 10 | Slot thành segment với `src_zh=""` và `on_screen`; version và round; script rỗng; liên hệ mới thì `needs_human`; slot lệch thì bị từ chối; `vbee_stt` đọc vocals, phân loại lỗi, vocals mất, thiếu credential |
| `tests/activities/test_review_dub.py` | 7 | Không gọi Jev; tràn slot là issue `dub` của đúng dòng; loudness đo trên track; không có nguồn thì chỉ kiểm chính dòng; track mất; dọn thư mục tạm; signature |
| `tests/starter/test_vbee_starter.py` | 4 | Start `VbeeDubbingWorkflow` với cùng workflow id, voice là marker, canary không có trong history, stub VoiceStudio không nhận request nào; thiếu setting thì `CONFIG_ERROR`, row giữ nguyên; giới hạn tính chung cả hai workflow type (dùng starter mới để bộ nhớ "vừa start" không che kết quả); mặc định là `voicestudio` |
| `tests/agents/test_slot_shortener.py` | +4 | `ids` hỏi dòng có ước lượng vẫn vừa và không hỏi dòng khác; câu trả lời vẫn phải vừa ở tốc độ được hỏi; id không có trong plan; không có `ids` thì giữ hành vi cũ |
| `compose-constraints.spec.ts` | 22/22 | Allowlist mới. Test đỏ từ trước (`worker-agents` thiếu `LOCALIZER_MODE`...) đã được sửa. Thêm kiểm: `VBEE_*` không tới `worker-media` |

**Mutation tay.**
- Script nằm ở `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/vbee-mut/mutate.py`. Script làm hỏng từng guard một, chạy đúng các test liên quan, rồi khôi phục file và kiểm lại bằng hash.
- 16 guard bị phá: `clip` không resolve; hash bỏ qua voice cấu hình; bỏ hiệu chỉnh tốc độ; chạy lại sau review mà không có `ids`; dùng tốc độ nhanh nhất thay cho chậm nhất; starter chỉ đếm một workflow type; starter đưa id thật vào input; starter hỏi VoiceStudio ở chế độ vbee; router bỏ qua Vbee; không bắt buộc marker; `to_ask` bỏ qua `ids`; bỏ kiểm liên hệ; `review_dub` gọi Jev; `vbee_stt` chuyển sang queue `io`; bỏ `_record_failure`; worker bỏ kiểm Vbee khi khởi động.
- Cả 16 lần đều làm test đỏ.
- Ở lượt đầu, mutation "chạy lại sau review mà không có `ids`" vẫn xanh, vì dữ liệu test chưa phân biệt được hai cách chọn dòng. Tôi đã sửa test (dòng 0 có 12 âm tiết) để test đỏ khi bị phá.

## Lưu ý

1. **Starter nhận credential Vbee chỉ để kiểm có hay không.** Đây là yêu cầu "kiểm Vbee settings có mặt". Đổi lại, token và voice id có mặt trong thêm một container. Nếu muốn chặt hơn, có thể bỏ ba biến này khỏi starter và dựa vào việc worker `agents` từ chối khởi động khi thiếu.
2. **Mỗi retry STT hoặc TTS là một job Vbee bị tính phí.** `vbee_stt` retry tối đa 4 lần; TTS dùng chính sách của `synthesize_segments` (8 lần).
3. **Giới hạn 10 s của slot vẫn là giới hạn mềm** (phase 2). Một slot dài tràn sẽ đi qua shortener, rồi qua review.
4. **Tốc độ dùng khi rút gọn sau review là tốc độ chậm nhất trong các dòng bị nêu.** Một dòng đọc nhanh hơn trong cùng nhóm sẽ bị cắt sâu hơn mức cần. Đây là đánh đổi để giữ đúng một request LLM.
5. **Activity `voice_rate` vẫn chạy cho video không có lời.** Kết quả được cache theo voice, nên chỉ tốn ở lần đầu.
6. **Phần lớn test được viết sau code, không theo thứ tự red-first.** Tôi bù lại bằng một lượt mutation tay ở cuối, mô tả ở phần sau.
7. **Run cũ còn đang chạy sau khi chuyển sang `vbee`.** Worker vẫn phục vụ chúng nếu VoiceStudio còn được cấu hình. Nếu bỏ `VOICESTUDIO_URL`, các run cũ đó sẽ fail.

## Kiểm tra cuối

- `uv run pytest -q` (cả bộ): 1721 passed, 7 deselected. Trước khi làm là 1628.
- `uv run ruff check .`: sạch. `uv run ruff format --check .`: 247 file đã đúng format.
- `cd services/downloader && pnpm exec jest test/compose-constraints.spec.ts`: 22/22.
- Replay: 262 test trong `tests/replay` đều xanh. Trong đó, mọi bộ history cũ replay được với `VideoLocalizationWorkflow`, và 7 history mới replay được với `VbeeDubbingWorkflow`.

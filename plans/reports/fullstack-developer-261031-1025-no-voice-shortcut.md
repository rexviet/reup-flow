# Đường tắt cho video không có giọng nói (`published_original`)

## Kết quả

Bước kiểm tra giọng nói đã chạy ở đầu `VideoLocalizationWorkflow._pipeline`, trước `_prepare`, nghĩa là trước cả `ingest` (Demucs) và `voice_rate`. Activity mới `check_voice` tải video gốc, giải mã âm thanh thành 16 kHz mono bằng ffmpeg, rồi cho Silero VAD đo phần âm thanh là tiếng nói. Nếu phần đó dưới 5% (là setting) và file có luồng video, run kết thúc ngay: video gốc được sao chép nguyên trong bucket sang `output/{aweme_id}.mp4`, và activity `publish` ghi `localization.runs.status = 'published_original'`. Mọi trường hợp khác vẫn chạy như hôm nay: video có giọng, video đúng bằng ngưỡng, file không có luồng video, kiểm tra hỏng hẳn (fail open), và khi tắt cổng.

Toàn bộ test của service đều xanh: 3197 passed, 7 deselected (là các test `live`). Ruff sạch. Lịch sử cũ replay không đổi, bộ lịch sử mới replay được, còn workflow control "ungated" bị `NondeterminismError` trên cả 51 lịch sử cũ.

Kiểm tra live dùng chính hàm của code mới và model thật, chạy trên 215 video nguồn:
- 121 video dưới 5%, khớp với spike.
- **117 video đi đường tắt.** 4 video dưới 5% không đi đường tắt vì là file chỉ có tiếng, không có luồng video. Chúng vẫn đi tiếp và `ingest` từ chối như hôm nay.
- 98 video đi tiếp.
- Mọi video có tiếng Trung nói thật đều đi tiếp.

Chưa có video thật nào được chạy qua workflow.

## Patch id

`VOICE_CHECK_PATCH = "publish-originals-without-voice"` nằm trong `workflows/video_localization.py`. Việc kiểm tra marker đi qua `_checks_for_voice()`, giống `_asks_about_ads()`, để control `UngatedVoiceCheckWorkflow` có thể bỏ marker đi.

## Các file đã đổi

Thư mục gốc là `services/localization/`, trừ hai file cấu hình ở gốc repo.

Mã nguồn:
- `src/localization/activities/check_voice.py` (mới):
  - port `SpeechDetector` với một hàm `speech_seconds(pcm) -> float`;
  - `SileroSpeechDetector` (Silero VAD, ngưỡng 0.5, `min_speech_duration_ms=250`, đúng cấu hình của spike);
  - các hàm thuần `speech_share`, `audio_seconds`, `decode_pcm`, `measure`;
  - activity `CheckVoiceActivity.check_voice(spec) -> VoiceCheck`.
- `src/localization/contracts/models.py`:
  - thêm `VoiceCheck(speech_share: float 0..1 | None = None, has_video: bool = True, no_voice: bool = False)`;
  - `PublishOutcome` có thêm `"published_original"`;
  - `LocalizationResult.prep` thành `PrepResult | None = None`;
  - cập nhật docstring của `PublishRequest` và `LocalizationResult`.
- `src/localization/workflows/video_localization.py`:
  - thêm queue `VOICE_CHECK_QUEUE = "voice_check"`, patch id, và chính sách `VOICE_CHECK`: start_to_close 10 phút, heartbeat 2 phút, retry 3 lần từ 5 s, không retry lỗi `_PERMANENT`;
  - thêm `_checks_for_voice`, `_has_no_voice` (bắt `ActivityError` rồi trả False, tức fail open; quyết định là `no_voice and has_video`) và `_publish_original`.
- `src/localization/activities/publish.py`: thêm nhánh `published_original`.
  - Từ chối khi có `render_key`, `verdict`, `reasons` hoặc `dub`.
  - Sao chép `spec.video_key` sang `output_key(aweme_id)` bằng `ObjectStore.copy` với `produced_by="download"`.
  - Không gửi score lên Langfuse.
- `src/localization/config.py`: thêm `voice_check_enabled: bool = True`, `voice_check_min_speech_share: float = 0.05` (ràng buộc 0..1) và `voice_check_concurrency: Concurrency = 2`.
- `src/localization/workers/run.py`: thêm `QueueSpec("voice_check", settings.voice_check_concurrency, [check_voice])` trên worker `agents`.

Phụ thuộc:
- `pyproject.toml` và `uv.lock`: thêm `silero-vad==6.2.3` và `torch==2.5.1`; `torch` chỉ lấy từ index CPU của PyTorch (`[tool.uv.sources]` cùng `[[tool.uv.index]] pytorch-cpu`, `explicit = true`).

Cấu hình:
- `.env.example` và `docker-compose.yml` (service `worker-agents`):
  - thêm `VOICE_CHECK_ENABLED` và `VOICE_CHECK_MIN_SPEECH_SHARE`, đặt cạnh `AD_GATE_*`;
  - thêm `VOICE_CHECK_CONCURRENCY` cạnh `IO_CONCURRENCY`;
  - cập nhật chú thích danh sách queue.
- File `.env` không bị đụng tới.

Tests mới:
- `tests/support/voice_fakes.py`: chạy activity thật trên một detector giả. Video nguồn là file mp4 thật do ffmpeg tạo, có ba loại: có hình và tiếng, chỉ hình, chỉ tiếng.
- `tests/activities/test_check_voice.py`
- `tests/workflows/test_voice_check.py`
- `tests/replay/ungated_voice_check_flow.py`
- `tests/replay/histories/publish-originals-without-voice/*.json` (4 file)

Tests đã sửa (không test nào bị xóa hay nới lỏng):
- `tests/support/prep_worker.py`: đăng ký `check_voice` trên queue `voice_check`. Mặc định detector "nghe thấy giọng", và activity dùng chung bucket với `publish`.
- `tests/replay/record_histories.py`: thêm bộ kịch bản mới.
- `tests/replay/test_replay.py`, `tests/activities/test_publish.py`, `tests/observability/test_run_scores.py`, `tests/workers/test_agent_wiring.py`.
- Các file liệt kê thứ tự activity hoặc danh sách queue: `tests/workflows/test_prep_stage.py`, `tests/workflows/test_ad_gate.py`, `tests/agents/test_localizer_workflow.py` (chi tiết ở phần Test).

## Chọn queue và lý do

Tôi tạo một queue mới là `voice_check` (mặc định 2 slot), do worker `agents` phục vụ. Tôi không dùng `media` hay `io`.

- `media` có 2 slot và đầy job Demucs, mỗi job 60 đến 100 s. Đặt check ở đó thì một tác vụ dưới một giây phải chờ sau chúng, đúng điều cần tránh.
- `io` cũng không nhẹ. Theo `.env.example`, nó chở `detect_subtitles`: mỗi activity giữ một chunk frame trên đĩa, gửi tối đa 8 request vision, timeout 45 phút. Nó còn chở các tool của agent và `publish`, chỉ với `IO_CONCURRENCY=2`. Vì mọi run đều bắt đầu bằng bước check, check phải có queue riêng thì mới không xếp hàng sau các việc đó.
- Đặt trên worker `agents` thay vì `media`:
  - Hai worker cùng image nên đều có torch.
  - Một lỗi ở tiến trình `media` có thể giết các job Demucs đang chạy dở, mà job đó đắt.
  - `/tmp` của `agents` là tmpfs 1 GB. Video nguồn lớn nhất của kênh khoảng 88 MB, nên với 2 slot vẫn đủ chỗ.
- `torch` và `silero_vad` chỉ được import khi có lần đo đầu tiên. Vì vậy tiến trình `media`, dù cũng import `workers/run.py`, không nạp torch cho việc này.
- `silero_vad` tự đặt `torch.set_num_threads(1)` khi import. Mỗi lần đo dùng một luồng CPU, nên tranh CPU với Demucs rất ít.
- Model giữ trạng thái giữa các chunk, nên `SileroSpeechDetector` khóa để mỗi lúc chỉ một luồng dùng model. Việc tải và giải mã của hai slot vẫn chạy song song.

## Sao chép và ghi run thế nào, và vì sao

- Quyết định nằm ở workflow: `check.no_voice and check.has_video`.
  - Activity tự so ngưỡng, vì ngưỡng là setting của worker, giống `classify_video`. Activity trả cả phần tiếng nói lẫn `has_video`.
  - File chỉ có tiếng vẫn được đo và trả `has_video=False`. Workflow cho nó đi tiếp tới `ingest`, nơi nó bị từ chối với `no_video_stream` như hôm nay.
  - File không có luồng âm thanh có phần tiếng nói bằng 0, nên đi đường tắt.
- Ghi run bằng chính activity `publish` với `PublishRequest(outcome="published_original")`. Lý do giống hệt `skipped_ad`: đó là nơi duy nhất ghi `localization.runs`, đã có upsert idempotent, `llm_cost_usd`, retry `PUBLISH`, và đường `_record_failure` nếu chính bước publish hỏng.
- Khóa nguồn **không** đi qua `render_key`, và tôi không thêm field mới. `publish(spec, request)` đã nhận `spec`, và `spec.video_key` chính là video gốc. Lý do không dùng `render_key`:
  - Gán vào `render_key` sẽ không trung thực, vì đó không phải bản render.
  - Nhánh mới còn từ chối mọi request có `render_key`.
  - Hợp đồng `PublishRequest` giữ nguyên, nên các request cũ vẫn hợp lệ.
- Bytes không đi qua tiến trình: dùng `ObjectStore.copy`, tức S3 `CopyObject`, giống cách publish sao chép bản render.
  - Nguồn mất thì ra `ArtifactMissingError(..., "download")`. Đây là lỗi permanent, run ghi `failed`.
- Row của một run đi đường tắt: `status='published_original'`, `output_key='output/{aweme_id}.mp4'`, `review_key=NULL`, `needs_human=false`, `review_rounds=0`, `verdict={"reasons": [], "verdict": null}`.
  - Đã chạy thật trên Postgres scratch với migration thật.
  - Schema không đổi: `status` là `text NOT NULL`, không có CHECK (`migrations/261026-localization-core.sql`). Vì không đổi schema nên không cần backup.
- Kết quả workflow là `LocalizationResult(prep=None, script=None, dub=None, publish=PublishResult(outcome="published_original", output_key=...))`.
  - `prep` có mặc định None, nên result cũ vẫn parse được (có test).
  - Trong `src/` và `eval/` không có ai đọc `result.prep`.
  - `eval/build_dataset.py` lọc `status = ANY(...)`, mặc định là `published`, nên run mới không lọt vào dataset.
  - Ngoài service này không có ai đọc `localization.runs`.
- Score Langfuse không được gửi, giống `skipped_ad`. `run_scores` luôn sinh `review_rounds`, `needs_human` và `safety_flag`. Với run này các giá trị đó sẽ là 0/false và đọc thành "video ra ngoài mà không ai review", trong khi thực ra không có gì được localize. Test `test_a_published_original_sends_no_scores` khóa điều này. File `observability/scores.py` không cần đổi.

## Phụ thuộc và kích thước image

- `silero-vad==6.2.3` là bản resolver chọn cho `uv run --with silero-vad`, cũng là bản spike đã dùng.
  - Phụ thuộc bắt buộc chỉ có `packaging` và `torch>=1.12`. `torchaudio`, `onnxruntime` và `numpy` chỉ là extra.
  - Wheel nặng 11.3 MB và chứa sẵn trọng số trong `silero_vad/data/`: `silero_vad.jit` 2.27 MB cùng các bản ONNX, tổng khoảng 14 MB khi giải nén.
  - `load_silero_vad()` đọc file qua `importlib.resources` và không tải gì lúc chạy. Hàm có `torch.hub.download_url_to_file` chỉ nằm trong class `Validator`, mà class này không được dùng.
- `torch==2.5.1` lấy từ `https://download.pytorch.org/whl/cpu` (index explicit). Lock chỉ có hai biến thể:
  - `torch 2.5.1` cho linux aarch64 và macOS;
  - `torch 2.5.1+cpu` cho linux x86_64 và Windows.
  - Lock không có gói `nvidia-*` hay `triton` nào.
  - Tôi ghim đúng bản mà Dockerfile cài cho Demucs. Như vậy bước `uv pip install torch==2.5.1 torchaudio==2.5.1 demucs==4.0.1` sau đó thấy torch đã thỏa và không đổi gì.
  - Nếu không chỉ index CPU, PyPI sẽ chọn torch có CUDA trên Linux.
- Image hiện tại (kiểm tra bằng `docker exec` chỉ đọc vào `reup-flow-worker-media-1`) đang có `torch 2.5.1` (aarch64, `cuda None`), `numpy` và các phụ thuộc của torch (sympy 1.13.1, networkx 3.7, jinja2, fsspec, mpmath). Nó chưa có `silero-vad`.
  - Image có một build definition dùng chung cho cả ba service, nên `worker-agents` có cùng torch.
- Image tăng khoảng 14 MB, chủ yếu là `silero-vad`. Torch (khoảng 375 MB) chỉ chuyển từ layer Demucs sang layer `uv sync`, tổng không đổi. Lock còn kéo theo `filelock 4.2.5` và `setuptools 84.2.1`, mới hơn chút so với bản image đang có.
- Môi trường dev giờ cũng có torch 2.5.1. Vì thế test chạy được đường Silero thật, đổi lại có một UserWarning "Failed to initialize NumPy" vì dev env không có numpy; image thì có.

Chưa xác minh: tôi chưa build image. Dockerfile không nằm trong danh sách file được sửa và cũng không cần sửa, nhưng chưa chứng minh được `uv sync --frozen` trong Docker đi qua index CPU suôn sẻ. Bằng chứng hiện có là `uv export --frozen --no-dev` cho ra đúng marker nêu trên.

## Test: lệnh và kết quả

Tất cả chạy trong `services/localization`.

- `uv run pytest -q tests/activities/test_check_voice.py`: **18 passed**.
  - Logic trên detector giả:
    - dưới ngưỡng thì `no_voice`, đúng bằng ngưỡng thì có giọng;
    - ngưỡng lấy từ tham số;
    - file không có âm thanh: phần tiếng nói bằng 0 và detector không được gọi;
    - file không có hình: vẫn đo, và trả `has_video=False`;
    - tắt cổng thì không đọc gì;
    - share bị kẹp trong 0..1.
  - Lỗi:
    - mất nguồn ra `ArtifactMissingError` với stage `download`;
    - file hỏng ra permanent;
    - lỗi của detector giữ nguyên loại (permanent hoặc transient).
  - Hợp đồng: result cũ vẫn parse được, và `VoiceCheck` từ chối share ngoài 0..1.
  - Settings: mặc định là bật, 0.05 và 2 slot; giá trị ngoài 0..1 bị từ chối; giá trị đọc được từ env.
  - **Silero thật:** im lặng cho 0 giây tiếng nói, sóng sin 440 Hz cho 0, input rỗng hoặc lẻ byte cho 0. Thêm một lượt đầu-cuối qua activity thật (tải, probe, giải mã, model thật) trên một mp4 chứa tone ra `VoiceCheck(0.0, has_video=True, no_voice=True)`.
  - Repo không có clip giọng nói thật nào, và tôi không tự dựng "giọng nói" từ tone. Phía có giọng của model thật được xác nhận bằng kiểm tra live bên dưới.
- `uv run pytest -q tests/workflows/test_voice_check.py`: **7 passed**, chạy trên môi trường test Temporal, có đọc history.
  - Video không giọng: các activity đã lên lịch đúng bằng `[("check_voice", "voice_check"), ("publish", "io")]`, tức không có `ingest`, `voice_rate`, `transcribe`, `detect_subtitles`, `classify_video` hay `agent__*`, và `PrepFakes` không được gọi lần nào.
    - Row là `published_original`, `output` là `output/7001.mp4`, `review` None, `needs_human` False, rounds 0.
    - `output/7001.mp4` trùng từng byte với file nguồn.
    - Không có bản render, không có review, không có `review/`.
  - Có giọng (40%), đúng ngưỡng (5% so với 5%), kiểm tra hỏng permanent (gọi đúng 1 lần), hết lượt retry transient (gọi đúng 3 lần), tắt cổng: tất cả kết thúc `published`. `check_voice` chạy đúng một lần, đầu tiên, trên queue `voice_check`; theo sau là phần chuẩn bị; output là bản render chứ không phải video gốc.
  - File không hình, 0% tiếng nói: không được publish. Run đi tiếp tới `ingest` (ở đây ingest được giả lập lỗi `no_video_stream` như ingest thật), thất bại, row ghi `failed`, và không có gì ở `output/`.
- `uv run pytest -q tests/activities/test_publish.py tests/observability/test_run_scores.py`: **38 passed**, có Postgres scratch thật. Các test mới:
  - video gốc được sao chép sang output, nguồn vẫn còn nguyên;
  - từ chối khi có render, verdict hoặc reasons (3 trường hợp);
  - mất nguồn ra lost download;
  - bảng thật nhận `published_original`;
  - không gửi score.
- `uv run pytest -q tests/workers`: **83 passed**. Test mới kiểm tra rằng `check_voice` chỉ được phục vụ trên `voice_check`, dùng limit, ngưỡng và công tắc từ settings, detector mặc định là Silero, và không có ở `media`, `io` hay queue nào khác.
- `uv run pytest -q tests/replay -p no:randomly`: **404 passed**.
  - Toàn bộ 55 lịch sử replay được trên code mới, gồm 51 lịch sử cũ và 4 lịch sử mới (`original-published`, `voice-localized`, `check-fails-for-good`, `no-picture-fails-in-the-ingest`).
  - Test về bộ lịch sử mới kiểm tra: marker có mặt; `check_voice` là activity đầu tiên, trên `voice_check`; `original-published` chỉ có check rồi publish với `outcome="published_original"`.
  - Lịch sử cũ được xác nhận là không có marker và đều bắt đầu bằng `ingest`.
  - Control `UngatedVoiceCheckWorkflow` bị `NondeterminismError` trên **cả 51** lịch sử cũ.
- `uv run ruff check src tests` và `uv run ruff format --check src tests`: sạch.
- Toàn bộ suite, `uv run pytest -q -p no:randomly`:
  - Lần 1: 3196 passed, 1 failed, 7 deselected (là các test `live`). Test hỏng là `test_localizer_workflow.py::test_the_stage_comes_after_preparation...`, vì nó ghim thứ tự activity, đã sửa như mô tả bên dưới.
  - Lần 2 sau khi sửa: **3197 passed, 7 deselected**, 4 warning (3 warning có sẵn từ trước và 1 warning NumPy của torch trong dev env), mất 703 s.
- Đột biến thủ công (không phải mutation harness), mỗi lần sửa xong đều khôi phục và đối chiếu bằng `cmp`:
  - bỏ `and check.has_video`: test file không hình đỏ;
  - bỏ `published_original` khỏi nhánh chặn score: test score đỏ;
  - đổi `<` thành `<=`: 2 test ngưỡng đỏ;
  - bỏ `workflow.patched` (luôn check): 55 replay đỏ.
  - Lần đầu, đột biến `has_video` không làm test nào đỏ, vì activity chặn trùng một lớp. Tôi đã sửa thiết kế: activity luôn đo, còn workflow giữ quyết định `has_video`. Sau đó test mới đỏ khi đột biến.

Các test cũ phải sửa (không nới lỏng; đều chặt hơn hoặc thêm phần tử vào danh sách liệt kê):
- `test_prep_stage.py`:
  - `test_preparation_runs_each_stage_once_on_its_own_queue` giờ ghim `[:5]` bắt đầu bằng `("check_voice", "voice_check")`;
  - `test_queue_limits_come_from_settings` thêm `voice_check`;
  - test tên activity đã đăng ký thêm `check_voice`;
  - hai test tự dựng worker được thêm một worker `voice_check` (detector "có giọng"); nếu không, run sẽ treo ở queue mới.
- `test_ad_gate.py`: `PREPARATION` thêm `check_voice`, vì đây là các activity chạy trước `classify_video`.
- `test_localizer_workflow.py`: `names[:3] == ["check_voice", "ingest", "voice_rate"]`.
- `test_replay.py`:
  - thêm set mới vào các danh sách "lịch sử ghi trước X", giống lần thêm `skip-advertisements`;
  - ba control (rate cạnh ingest, `localizer_context`, ad check) loại `original-published` và `no-picture-fails-in-the-ingest`, vì hai run này kết thúc trước phần chuẩn bị hoặc hỏng trong phần đó nên phát cùng lệnh dù có marker hay không. Lý do giống với `voice-rate-fails-for-good` và `advertisement-skipped` vốn đã bị loại sẵn.
- Không có test nào liệt kê đủ các `PublishOutcome`, nên không phải sửa test vì lý do đó.

## Kiểm tra live (chỉ đọc)

Script nằm ở scratchpad: `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/e44d7982-5bf6-4765-9acd-e47d92746115/scratchpad/live_voice_check.py`.
- Nó gọi chính `measure()` và `SileroSpeechDetector` của code mới, ngưỡng lấy từ `Settings().voice_check_min_speech_share` (0.05), và quyết định giống workflow (`share < 0.05 and has_video`).
- Đầu vào là các file trong `data/douyin/MS4wLjABAAAAjNPQW_CtRZQtt3mzKoOUpvn_FIo0RIOz3wFsoeNz6Rc/`.
- 215 file mất 59.9 s, tính cả lần nạp model, tức khoảng 0.28 s mỗi video (không tính thời gian tải từ MinIO).

(a) 28 video có nhãn:
- 7 video "none" đều đo được 0.0% và **cả 7 đi đường tắt**.
- 21 video "speech": 9 đi đường tắt, 12 đi tiếp.
  - 9 video đi đường tắt gồm đúng 8 video 0.0% (7475199881016020284, 7480763442807196988, 7552039043057356090, 7565005737722907953, 7652576736073102842, 7659747539289223674, 7669283674021513061, 7671988475562465273) và video 3.7% (7663728816821922993).
  - Kết quả khớp đúng tập kỳ vọng: 16 video đi đường tắt.
  - Mọi video có tiếng Trung nói thật (từ 25.1% trở lên) đều đi tiếp. Ba video nhãn "speech" ở 8.2%, 8.8% và 18.9% (7661932302109375409, 7658979077479535077, 7647460922278009273) cũng đi tiếp, vì nằm trên 5%.

(b) Toàn bộ 215 video:
- 108 video đúng 0%, 13 trong khoảng (0%, 5%), 17 trong [5%, 25%), 77 từ 25% trở lên. Phân bố trùng với spike, nên **121 video dưới 5%**.
- **117 video đi đường tắt, 98 đi tiếp.**
  - 4 trong 121 video dưới 5% không có luồng video (7389167757058772251, 7441427591061392700 và 7531598489367121210 ở 0%, 7575023855045328251 ở 2.6%). Đây là file chỉ có tiếng (aac hoặc mp3). Chúng đi tiếp và sẽ hỏng ở `ingest` với `no_video_stream` như hôm nay.
  - 2 file không có hình còn lại (13.0%, 20.6%) cũng đi tiếp.
- Các video gần ngưỡng (từ 4% đến 5.3%): 7414328184545479987 (4.7%), 7656296370752721061 (4.5%), 7635595666248862841 (4.1%), 7446990734089080124 (5.1%), 7598396587995682298 (5.3%).

## Đã xác minh và chưa xác minh

- **Đã xác minh:**
  - logic workflow trên môi trường test Temporal, có đọc history;
  - replay lịch sử cũ và mới, kèm control;
  - publish và row trên Postgres thật (DB scratch);
  - activity thật với ffmpeg và ffprobe thật;
  - Silero thật trên im lặng và tone;
  - quyết định thật trên 215 video thật bằng code mới;
  - wiring worker theo settings;
  - không có CUDA trong lock;
  - trọng số model nằm trong wheel.
- **Chưa xác minh:**
  - Chưa chạy video thật nào qua workflow, cũng chưa có worker thật nào chạy code mới. Tôi không động vào docker, Temporal hay Postgres thật.
  - Chưa build image, nên chưa đo kích thước thực tế và chưa thấy `uv sync --frozen` chạy qua index CPU trong Docker.
  - Chưa đo bước `CopyObject` trên MinIO thật.
  - Chưa chạy mutation harness, theo đúng quy định.

## Rủi ro

- Giọng nói nhỏ chìm dưới nhạc lớn không có trong mẫu có nhãn. Silero có thể cho dưới 5% với những video như vậy, và video có lời thật sẽ bị đăng nguyên bản. Năm video ở mức 4% đến 5.3% nên được nghe thử bằng tai.
- Một video đi đường tắt nhầm có thể sửa: chạy lại với `VOICE_CHECK_ENABLED=false` và tăng `generation`. Tuy vậy, bản gốc đã nằm ở `output/` trước đó.
- `output/` trước đây chỉ chứa bản render đã qua review. Giờ nó chứa thêm video gốc chưa qua review nào, theo đúng quyết định của user. Nơi nào tiêu thụ `output/` cần biết điều này. Hiện trong repo không có consumer nào ngoài service.
- Lời bài hát tiếng Anh, hoặc video "speech" ở 8% đến 19%, vẫn được localize như cũ. Ngưỡng 5% là bảo thủ, nên chỉ cắt các video gần như không có tiếng nói.
- Worker `agents` giờ nạp torch khi có lần check đầu tiên, tốn thêm vài trăm MB RAM và khoảng 7 s cho lần nạp đầu trên máy dev. Hai service không đặt giới hạn bộ nhớ.
- Lock giờ ghim `torch==2.5.1` từ index CPU, trùng với bản ghim cho Demucs trong Dockerfile. Ai nâng torch phải nâng cả hai chỗ.
- Khi deploy, các run đang chạy dở không có marker nên vẫn đi tiếp như cũ. Chỉ run mới mới đi qua bước check. Worker `agents` phải chạy code mới trước, nếu không queue `voice_check` sẽ không có ai phục vụ và các run mới sẽ chờ.
- `VbeeDubbingWorkflow` không có bước check này, vì nằm ngoài phạm vi.

## Câu hỏi chưa giải quyết

1. Có cần một báo cáo hay UI liệt kê các run `published_original` (và `skipped_ad`) để người duyệt nghe lại, nhất là những video ở mức 3% đến 5%, không?
2. Có muốn thêm `schedule_to_start_timeout` cho bước check không? Khi đó, nếu không có worker nào phục vụ `voice_check` (ví dụ deploy lệch), run sẽ fail open sau một khoảng ngắn thay vì chờ.
3. `VbeeDubbingWorkflow` có cần bước check tương tự không?
4. Dockerfile có nên ghi chú rằng torch giờ được ghim cả trong `pyproject.toml` không? Tôi không sửa Dockerfile vì nó nằm ngoài danh sách file được phép sửa.

## Bổ sung: file không có luồng video kết thúc ngay (`skipped_no_video`)

### Kết quả

File không có luồng video giờ kết thúc ngay ở bước kiểm tra đầu run. Activity `publish` ghi `status='skipped_no_video'`, run không đi tiếp tới `ingest`, và không có gì được sao chép sang `output/`. Quyết định dựa trên `has_video` và được xét **trước** phần tiếng nói, nên âm thanh có tiếng nói hay không cũng không đổi kết quả.

Toàn bộ suite xanh: **3200 passed, 7 deselected**, mất 685 s. Ruff sạch.

Kiểm tra live trên 215 video cho **6** video `skipped_no_video`, **117** `published_original` và **92** đi tiếp. Kỳ vọng là 4, 117 và 94. Chênh lệch nằm ở 2 file chỉ có tiếng mà phần tiếng nói đo được trên 5%; giải thích ở mục live bên dưới.

### Thay đổi
- `contracts/models.py`:
  - `PublishOutcome` có thêm `"skipped_no_video"`.
  - Docstring của `PublishRequest`, `LocalizationResult` và `VoiceCheck` được cập nhật.
- `activities/check_voice.py`: khi file không có luồng video, activity dừng ở bước probe. Nó không giải mã và không đo, rồi trả `VoiceCheck(speech_share=None, has_video=False, no_voice=False)`.
  - `speech_share=None` nghĩa là "không đo". Giá trị này trung thực hơn một con số 0 vốn ngụ ý đã nghe mà không thấy giọng. Nó cũng tránh tốn công giải mã cho một file đằng nào cũng bị bỏ qua.
  - `None` vẫn giữ nghĩa cũ khi cổng tắt. Hai trường hợp phân biệt được nhờ `has_video`: tắt cổng thì có video được giả định, file không hình thì `has_video=False`.
- `workflows/video_localization.py`: `_has_no_voice` và `_publish_original` được thay bằng hai hàm.
  - `_unprepared_end(spec) -> PublishOutcome | None` xét theo thứ tự: không có video thì `skipped_no_video`, `no_voice` thì `published_original`, còn lại trả None để đi tiếp. Kiểm tra hỏng thì cũng trả None (fail open).
  - `_end(spec, outcome)` gọi `publish` với outcome đó.
  - Thứ tự lệnh của nhánh đã patch vẫn là `check_voice`, `publish`.
- `activities/publish.py`: `skipped_no_video` đi chung nhánh "không có render" với `failed` và `skipped_ad`, nên không sao chép gì và từ chối nếu có `render_key`. Nó cũng không gửi score Langfuse, lý do giống `skipped_ad`: không có gì được làm ra, và `needs_human` bằng 0 sẽ đọc thành video ra ngoài mà không ai review.

### Patch id: giữ nguyên `publish-originals-without-voice`

Tôi mở rộng nhánh đã patch thay vì thêm patch id mới. Cách kiểm tra:
- `rg -uu` trên toàn repo, bỏ qua `.venv`: chuỗi marker chỉ xuất hiện trong mã nguồn workflow, trong `tests/replay/` (`record_histories.py`, `test_replay.py`), `.pytest_cache`, báo cáo này và log nội bộ `.git/ai`. Không có history nào ngoài thư mục test. Các history test lưu marker ở dạng base64 nên rg không thấy, nhưng chúng đều nằm trong `tests/replay/histories/`.
- `docker exec` chỉ đọc vào `reup-flow-worker-agents-1` và `reup-flow-worker-media-1`: grep marker trong `/app/src` ra 0 file, và `check_voice.py` không có trong image.
  - Hai container chạy từ `2026-10-30T06:19:38Z`, trước khi code này được viết.
  - Vì vậy chưa có worker thật nào có thể ghi marker này vào một history thật.

### Test

Đã sửa đúng theo yêu cầu:
- Test workflow "file không hình đi tiếp tới ingest" đổi thành `test_a_file_without_a_picture_is_skipped_before_anything_is_prepared`.
  - Detector được đặt để gọi 90% là tiếng nói, chứng tỏ nội dung âm thanh không đổi kết quả.
  - Ingest giả được đặt để từ chối file, nhưng không được gọi tới lần nào.
  - Các activity đã lên lịch đúng bằng `[("check_voice", "voice_check"), ("publish", "io")]`.
  - `PrepFakes` không được gọi, row là `skipped_no_video`, `output` và `review` đều None, `needs_human` False, rounds 0, không có gì ở `output/` hay `review/`, và detector không được gọi.
- Test activity tương ứng đổi thành `test_a_file_without_a_picture_says_so_and_is_not_heard`: kết quả là `VoiceCheck(None, has_video=False, no_voice=False)` và detector không được gọi.
- Lịch sử replay `no-picture-fails-in-the-ingest.json` bị xóa. Bộ `publish-originals-without-voice` được ghi lại, giờ có `original-published`, `voice-localized`, `check-fails-for-good` và `no-video-skipped`.
  - Test của bộ này kiểm tra rằng `no-video-skipped` chỉ có check rồi publish với `outcome="skipped_no_video"`, và kết thúc COMPLETED.
- Control `UngatedVoiceCheckWorkflow` vẫn `NondeterminismError` trên cả 51 lịch sử cũ.
- Control "rate cạnh ingest" loại thêm `no-video-skipped`, cùng lý do với `original-published`: run kết thúc trước phần chuẩn bị nên không có rate nào để dời.

Test mới:
- `test_publish.py`: row không sao chép gì, từ chối render, bảng Postgres thật nhận status mới (3 test).
- `test_run_scores.py`: không gửi score (1 test).

Không có test nào liệt kê đủ các `PublishOutcome`, nên không phải sửa test vì lý do đó.

Đột biến thủ công, mỗi lần sửa xong đều khôi phục và đối chiếu bằng `cmp`:
- bỏ nhánh `has_video` trong workflow: test file không hình đỏ;
- bỏ `skipped_no_video` khỏi nhánh chặn score: test score đỏ.

Kết quả:
- `tests/activities/test_check_voice.py`, `tests/workflows/test_voice_check.py`, `tests/activities/test_publish.py`, `tests/observability/test_run_scores.py`, `tests/workers`, `tests/architecture`: **181 passed**.
- `tests/replay`: **403 passed**. Số này ít hơn lần trước 1 test, vì control rate có thêm một tham số bị loại.
- Toàn bộ, `uv run pytest -q -p no:randomly`: **3200 passed, 7 deselected**, 4 warning như trước, mất 685 s.
- `ruff check` và `ruff format --check`: sạch.

### Kiểm tra live (code mới, model Silero thật, 215 file mất 51.1 s)

Script ở scratchpad: `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/e44d7982-5bf6-4765-9acd-e47d92746115/scratchpad/live_voice_check2.py`. Nó gọi `measure()` của code mới và xét theo đúng thứ tự của workflow.

- `skipped_no_video`: **6**. Gồm 7389167757058772251, 7441427591061392700, 7452646214857788731, 7518711366992743739, 7531598489367121210 và 7575023855045328251.
- `published_original`: **117**, không đổi.
- Đi tiếp: **92**.
- Không có lần kiểm tra nào hỏng.

So với kỳ vọng 4/117/94: ngoài 4 file chỉ có tiếng mà người dùng đã nêu, còn **2 file chỉ có tiếng khác là 7452646214857788731 và 7518711366992743739**. Lần live trước đo được 13.0% và 20.6% tiếng nói cho hai file này. Vì trên 5%, chúng không nằm trong nhóm 121 video dưới ngưỡng, và lần trước chúng đi tiếp tới `ingest`. Lần live trước đã liệt kê cả 6 file trong mục "no video stream". Theo quy tắc mới (bỏ qua bất kể âm thanh), cả 6 đều kết thúc `skipped_no_video`. Tổng vẫn đúng: 6 + 117 + 92 = 215.

### Câu hỏi chưa giải quyết (bổ sung)
5. Hai file chỉ có tiếng mà phần tiếng nói khá cao (13.0% và 20.6%) giờ cũng bị bỏ qua. Đúng ý user là "bất kể âm thanh", nhưng nên xác nhận lại rằng không muốn giữ những post này.

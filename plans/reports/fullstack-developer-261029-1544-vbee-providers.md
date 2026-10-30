# Phase 1: provider Vbee STT và TTS cho production

Ngày: 2026-10-29. Trạng thái: xong, còn một việc nhỏ nằm ngoài phạm vi file của tôi (xem "Vấn đề").

## Kết quả

`VbeeStt` và `VbeeTts` đã chạy được với API thật qua đúng đường mà worker sẽ dùng (`from_settings`). Lần smoke test duy nhất gọi 2 lần thật (1 TTS, 1 STT) và cả hai đều thành công. Output không chứa token, app id hay voice id. Tôi đã kiểm tra bằng script so với giá trị trong `.env`, và script không in giá trị nào ra.

## File đã đổi

| File | Thay đổi |
|---|---|
| `services/localization/src/localization/providers/vbee.py` | Mới, 393 dòng |
| `services/localization/src/localization/config.py` | Thêm 5 setting Vbee và kiểm `VBEE_BASE_URL` trong `model_post_init` (+10) |
| `services/localization/src/localization/contracts/voice.py` | Thêm `"vbee"` vào `Provider`, thêm `VbeeSettings`, đăng ký vào `SETTINGS_BY_PROVIDER` và union của `VoiceRef.settings` (+10/-2) |
| `services/localization/tests/providers/test_vbee.py` | Mới, 47 test |
| `services/localization/tests/test_secret_canary.py` | Thêm phần canary của Vbee, 15 test |
| `services/localization/eval/vbee_provider_smoke.py` | Mới, script smoke chạy tay |

Tôi không sửa `providers/base.py`, vì các lỗi có sẵn trong `providers/errors.py` đã đủ dùng.

## Public API của `providers/vbee.py`

```python
class VbeeClient:
    def __init__(self, base_url: str, app_id: str, token: str, *, timeout_seconds: float,
                 http: httpx.AsyncClient | None = None) -> None
    @classmethod
    def from_settings(cls, settings: Settings, http: httpx.AsyncClient | None = None) -> VbeeClient
    async def call(self, name: str, span: Span, method: str, path: str, **kwargs) -> Any   # JSON của câu trả lời 2xx
    async def download(self, name: str, span: Span, url: str) -> bytes                     # theo redirect, không gửi header nào

class VbeeStt:
    def __init__(self, client: VbeeClient, *, poll_interval_seconds: float = 3.0,
                 poll_timeout_seconds: float = 300.0) -> None
    @classmethod
    def from_settings(cls, settings, http=None, *, poll_interval_seconds=3.0) -> VbeeStt
    async def transcribe(self, wav: bytes) -> SpeechTranscript

class VbeeTts:                       # thỏa protocol TtsProvider
    def __init__(self, client: VbeeClient, *, poll_interval_seconds: float = 1.0,
                 poll_timeout_seconds: float = 300.0) -> None
    @classmethod
    def from_settings(cls, settings, http=None, *, poll_interval_seconds=1.0) -> VbeeTts
    async def synthesize(self, text: str, voice: VoiceRef) -> bytes

async def stt_wav(audio: bytes) -> tuple[bytes, int]   # WAV gửi cho STT và độ dài tính bằng ms
```

**STT.**
- Nếu input là WAV PCM16 mono ở một sample rate Vbee nhận (8k, 16k, 22.05k, 24k, 32k, 44.1k, 48k) thì gửi nguyên như vậy. Mọi WAV khác được chuyển sang 16 kHz mono bằng `activities.ffmpeg.to_stt_audio`, chạy trong thread. Bytes không phải WAV bị từ chối ngay, trước khi gọi API.
- File trên 100 MB là lỗi vĩnh viễn.
- `audio_ms` lấy từ header của WAV được gửi đi.
- Mỗi utterance `{text, startTime, endTime}` (giây) thành `Utterance` (ms, làm tròn). Utterance có text rỗng bị bỏ.
- Một `COMPLETED` không có utterance nào cho ra transcript rỗng.

**TTS.**
- Body gửi đi: `text`, `voiceCode` = `voice.voice_id`, `mode=async`, `outputFormat=wav`, `speed` = `VbeeSettings.speed`, `webhookUrl=https://example.com/vbee-tts-callback`.
- Không ép `sampleRate`, nên giọng clone trả về 24 kHz.
- Provider poll đến `COMPLETED`, rồi tải `audioLink` ngay, theo redirect và không kèm `Authorization` hay `App-Id`, vì link dẫn ra khỏi API của Vbee.
- Kết quả không phải RIFF/WAVE là lỗi vĩnh viễn, để không bao giờ bị cache như audio.
- Voice của provider khác bị từ chối trước khi gọi API.

**Span.**
- Mỗi lần `transcribe` có đúng một span `provider.stt`, `input_units` là số giây audio.
- Mỗi lần `synthesize` có đúng một span `provider.tts`, `input_units` là số ký tự.
- Hai span đều có `provider=vbee`, `http.status_code` của câu trả lời cuối, và `provider.outcome` khi lỗi: `timeout`, `connection_error`, `poll_timeout`, `job_failed`, `link_expired`, `http_error`.
- Span không có model, URL, body hay header.

## Phân loại lỗi

| Trường hợp | Lỗi |
|---|---|
| 408, 429, 5xx, timeout, lỗi kết nối | `TransientProviderError` |
| 400, 401, 403 (kể cả "Missing feature"), các 4xx khác của API | `PermanentProviderError` |
| Link audio trả 403, 404 hoặc 410 (link hết hạn) | `TransientProviderError` ("the audio link expired"). Lần retry sẽ tổng hợp lại câu đó. |
| Hết thời gian poll | `TransientProviderError` ("the job is not done in time") |
| Job ở trạng thái `FAILED` | `PermanentProviderError` ("the job FAILED CODE") |
| Response không phải JSON, thiếu id, sai shape, thiếu `audioLink`, audio không phải WAV | `PermanentProviderError` |
| Lỗi tạm thời **trong lúc poll** | Không dừng: tiếp tục poll đến hết hạn, vì bỏ dở là phải trả tiền thêm một job nữa. Nếu đến hạn mà vẫn lỗi thì ném lại lỗi tạm thời cuối cùng. |

Message lỗi chỉ gồm tên provider, HTTP status và `error.code` của Vbee, và chỉ khi code khớp `[A-Z][A-Z0-9_]{0,63}`. Riêng lỗi 403 "Missing feature: x" thì thêm tên feature, nếu tên đó khớp `[a-z0-9-]+`. Ví dụ: `vbee stt: HTTP 403 FORBIDDEN (missing feature stt-sync)`. Message trong body của Vbee không bao giờ được chép vào lỗi.

## Settings

| Field | Env | Kiểu, mặc định |
|---|---|---|
| `vbee_base_url` | `VBEE_BASE_URL` | `str`, `https://api.vbee.vn`. Được kiểm bằng `check_endpoint_url`: không cho http với hostname, không cho URL có credential. |
| `vbee_app_id` | `VBEE_APP_ID` | `SecretStr \| None` |
| `vbee_api_token` | `VBEE_API_TOKEN` | `SecretStr \| None` |
| `vbee_voice_id` | `VBEE_VOICE_ID` | `SecretStr \| None` |
| `vbee_poll_timeout_seconds` | `VBEE_POLL_TIMEOUT_SECONDS` | `float`, 300, trong khoảng (0, 3600] |

- Timeout của mỗi HTTP request dùng `provider_timeout_seconds` có sẵn.
- Nếu thiếu app id hoặc token thì `from_settings` báo `PermanentProviderError` ngay khi khởi động.
- `VoiceRef(provider="vbee", settings=VbeeSettings(speed=0.25..1.9))`.
- `vbee_voice_id` là `SecretStr`, vì kế hoạch xếp nó vào nhóm secret. Phase 4/5 phải gọi `.get_secret_value()` khi dựng `VoiceRef`.

## Test

- `tests/providers/test_vbee.py`: 47 test. Fake là `httpx.MockTransport`, mô phỏng đúng shape JSON thật của spike. Các test phủ:
  - STT pending rồi completed, có map ms, header và multipart;
  - WAV 16k gửi nguyên, stereo 44.1k được chuyển đổi, bytes không phải WAV bị từ chối;
  - job `FAILED`, poll quá hạn, vượt qua một lỗi 503 trong lúc poll, utterance hỏng, kết quả rỗng, thiếu `transcriptId`;
  - TTS: flow batch có redirect 302, body request, bản tải không gửi credential, link hết hạn (403 và 404), audio không phải WAV, `FAILED`, poll quá hạn, thiếu `audioLink`, voice của provider khác, khớp protocol;
  - 429 và 500/502/503 là tạm thời, `ConnectError`/`ReadTimeout` là tạm thời, 400/401/403 "Missing feature" là vĩnh viễn (mỗi trường hợp chạy cho cả STT và TTS);
  - message lỗi không chứa nội dung body bị echo;
  - settings: secret không hiện trong repr, http bị từ chối, thiếu credential, dựng được từ settings, giới hạn speed.
- `tests/test_secret_canary.py`: thêm 15 test.
  - Gieo canary vào `VBEE_API_TOKEN`, `VBEE_APP_ID` và `VBEE_VOICE_ID`.
  - Provider thật chạy trong một activity Temporal, qua `ProviderWorkflow` có sẵn, ở 13 kịch bản: TTS và STT, mỗi bên với 200, 401, 403, 500, timeout và job FAILED, cộng TTS với link hết hạn.
  - Body lỗi của fake echo lại header `Authorization`, `App-Id` và body request (có chứa voice id).
  - Sau mỗi kịch bản, test tìm canary trong history, span, stdout/stderr và log. Test cũng khẳng định đúng credential canary đã thực sự được gửi đi.
  - Có thêm một test control cho bộ quét và một test cho repr của settings.
- **Kiểm chứng guard bằng mutation tay.** Tôi cố ý làm hỏng từng guard, và mọi lần đều có test chuyển đỏ:
  - bản tải gửi kèm header;
  - bỏ nhánh link hết hạn;
  - bỏ việc vượt qua lỗi lúc poll;
  - chép message của body vào lỗi (bộ canary bắt được 6 lỗi);
  - bỏ bước chuyển đổi WAV;
  - đưa voice id vào span (canary bắt được 7 lỗi);
  - chép message của job FAILED vào lỗi (canary bắt được 2 lỗi).

  Sau mỗi lần, file được khôi phục và tôi đã kiểm bằng `cmp`.
- **Kết quả các lệnh:**
  - `uv run pytest -q tests/providers tests/test_secret_canary.py`: 174 passed.
  - `ruff check` và `ruff format --check` trên 6 file đã sửa: sạch.
  - Toàn bộ suite trừ `tests/mutations`: 1627 passed, 1 failed (xem "Vấn đề").
- Project không cấu hình mypy hay pyright.

## Smoke test thật

Lệnh: `uv run python eval/vbee_provider_smoke.py <scratchpad>/vbee/vi_9s.wav`. Tôi chạy đúng một lần, gồm 2 lần gọi thật cộng các lần poll.

| Lần gọi | Kết quả | Độ trễ |
|---|---|---|
| TTS, 49 ký tự ("Xin chào các bạn, hôm nay mình thử giọng đọc mới.") | OK, WAV 24000 Hz, mono, 16-bit, 2,59 s, 124.302 bytes | 4,16 s (submit, poll 1 s, tải về) |
| STT, clip tiếng Việt 9 s | OK, `audio_ms` 9000, 1 utterance `[0–9000 ms]`, nội dung giống spike ("ba mươi tuổi mà trông như hai mươi … bổ sung dinh dưỡng") | 35,78 s (poll 3 s) |

## Vấn đề

1. **`test_every_setting_is_documented_in_env_example` đang đỏ.**
   - Test này đòi mọi setting phải có trong `.env.example` ở gốc repo.
   - File đó không thuộc phạm vi tôi được sửa, và đã có thay đổi chưa commit từ trước. Vì vậy tôi không đụng vào.
   - Cần thêm 5 dòng sau. Phase 5 hoặc orchestrator có thể thêm:
     ```
     VBEE_BASE_URL=https://api.vbee.vn
     VBEE_APP_ID=
     VBEE_API_TOKEN=
     VBEE_VOICE_ID=
     VBEE_POLL_TIMEOUT_SECONDS=300
     ```
2. **Voice id sẽ nằm trong workflow history** nếu phase 4 truyền `VoiceRef` qua input của workflow, giống cách `VOICE_ID` đang làm hiện nay.
   - Provider không bao giờ đưa voice id vào lỗi, log hay span. Test canary chỉ dựng `VoiceRef` bên trong activity.
   - Phase 4 cần chọn một trong hai cách: dựng voice ở phía activity từ settings, hoặc chấp nhận voice id nằm trong history (đi qua codec có sẵn).
   - Ngoài ra, cache key của TTS có hash voice id. Đây chỉ là hash, không lộ giá trị.
3. **Retry tốn thêm điểm.**
   - Mỗi lần retry một lỗi tạm thời là một job mới bị tính phí: giọng clone tính x2, STT tính theo giây.
   - Với link hết hạn, Vbee cho gọi lại Get request để lấy link mới mà không bị tính phí. Tôi giữ đúng spec là coi trường hợp này là lỗi tạm thời và tổng hợp lại, chưa thêm bước lấy lại link.
4. **Độ trễ TTS cộng dồn theo số piece.** `Synthesizer._speak` đọc các piece của một dòng **lần lượt**, mỗi piece là một job batch khoảng 4 s. Một dòng có 3 piece mất khoảng 12 s. Phase 4 nên tính điều này khi đặt timeout và heartbeat cho activity.
5. **Giới hạn độ dài của utterance.**
   - `Utterance.text` tối đa 4000 ký tự, theo kiểu `Text` của contract. Một utterance dài bất thường, ví dụ cả video chỉ ra một khối, sẽ làm validation lỗi và thành `PermanentProviderError`.
   - Với video 60–90 s thì không sao: utterance dài nhất của spike khoảng 60 s, tức khoảng 1.500 ký tự.
   - Tôi không đổi contract.

## Câu hỏi còn mở

1. Việc thêm 5 dòng vào `.env.example` giao cho phase 5 hay để orchestrator làm ngay?
2. Phase 4 có muốn `VoiceRef` của Vbee đi qua workflow input, tức voice id nằm trong history, hay dựng voice phía activity?

# Dubber chế độ `code`: báo cáo triển khai

Kết quả: Dubber đã có chế độ thứ ba `dubber_mode="code"`, chạy được trong workflow thật trên Temporal test server: model viết Python, `run_code` chạy nó trong sandbox Monty, code gọi `get_segment` / `estimate_duration` / `synthesize` / `accept`, các lời gọi `synthesize` được `asyncio.gather` thành nhiều activity riêng trên queue `tts` và chạy chồng nhau thật, rồi trả về đúng `DubDecisions` như hai chế độ cũ. Mặc định vẫn là `single`; `single` và `tools` giữ nguyên hành vi (prompt `tools` giống từng byte, mọi history cũ vẫn replay). Chưa commit.

Bộ test đầy đủ của service: **2981 passed, 7 deselected, 0 failed** (9 phút 38 giây). Sau đó có một sửa nhỏ (mục 5, cuối); các bộ liên quan chạy lại sau sửa: 376 passed.

## 1. Thay đổi theo file

| File | Thay đổi |
|---|---|
| `services/localization/pyproject.toml`, `uv.lock` | Thêm `pydantic-ai-harness[codemode,temporal]==0.61.0`. Bản 0.61.0 resolve được với `pydantic-ai-slim==2.61.0` đang pin, nên **không phải nâng slim**. Kéo theo `pydantic-monty 1.4.0` (+ client/runtime) và vài gói phụ của harness (`ddgs`, `lxml`, `primp`, `click`, `json-repair`). |
| `src/localization/agents/dubber.py` | `DubberMode = Literal["single","tools","code"]`; `CODE_PROMPT_VERSION = "dubber-code-1"`; `CODE_INSTRUCTIONS`; `CALLS_PER_LINE`; guard mới `SpokenRewrites`; `build_dubber(..., mode="tools"|"code", max_code_calls=...)`. Prompt `tools` được dựng lại từ hai hằng `DATA_RULE` và `REVIEWER_NOTES` nhưng ra **đúng từng byte** như cũ (sha256 trước/sau đều `fc95ab62…a33b`). |
| `src/localization/agents/temporal.py` | Patch id mới `DUBBER_CODE_PATCH = "dubber-code-mode"`; contract `DubberSetup(mode, context)`; agent `dubber_code` (tên `dubber_code`); capability `ActivityFailureEndsRun`; `dubber_code_usage_limits`; `_dubber_setup` (chọn đường theo patch) và `run_dubber` dùng nó. |
| `src/localization/activities/agent_context.py` | Activity mới `dubber_setup` trả `DubberSetup`. `dubber_context` giữ nguyên cho run cũ; worker đặt `code` trả lời nó là "không có context" (tức tool loop). |
| `src/localization/config.py` | `dubber_mode: DubberMode` (nhận thêm `code`), `localizer_mode` vẫn chỉ `single`/`tools`. Mặc định không đổi: `single`. |
| `src/localization/workers/run.py` | Đăng ký plugin của `dubber_code` trên `llm`/`io`/`tts` và activity `dubber_setup` trên `io`. |
| `.env.example` | Chỉ thêm một câu mô tả giá trị `code` của `DUBBER_MODE` (biến đã có sẵn, `docker-compose.yml` đã truyền nó, không đụng). |
| `tests/support/dub_fakes.py`, `tests/support/prep_worker.py` | `CodeDubber` (model kịch bản viết code), `speak_every_line`, hai núm lỗi cho `FakeDubberPorts` (`speak_failures`, `segment_errors`); prep worker đăng ký `dubber_code` và `dubber_setup`. |
| `tests/agents/test_dubber_code.py` (mới, 19 test) | Guard, agent không durable trên sandbox thật, prompt, limits, `DubberSetup`, settings. |
| `tests/workflows/test_dubber_code_mode.py` (mới, 12 test) | Workflow thật trên Temporal test server. |
| `tests/activities/test_agent_context.py`, `tests/workers/test_agent_wiring.py` | Thêm test cho `dubber_setup` và cho việc worker thật đăng ký activity của `dubber_code`. |
| `tests/replay/record_histories.py`, `tests/replay/test_replay.py`, `tests/replay/histories/dubber-code-mode/*.json` (4 history mới) | Bộ history mới và test của nó; thư mục mới được thêm vào danh sách loại trừ của các test "history cũ thật sự chưa có X" (vì history mới đương nhiên có mọi marker hiện hành). Không test cũ nào bị xoá hay nới điều kiện. |

## 2. Guard "tối đa 3 lần synthesize mỗi segment" và nơi giữ trạng thái

`RewriteLimit` đếm `ToolCallPart` trong history nên mù với lời gọi từ code (đã tái hiện bằng test đối chứng: cùng agent, chỉ có `RewriteLimit` + `CodeMode`, code gọi 5 lần thì cả 5 lần đều nói). Chế độ `tools` vẫn dùng nguyên `RewriteLimit`, không đổi gì.

Chế độ `code` dùng `SpokenRewrites`: hook `before_tool_execute` đếm mỗi lời gọi `synthesize` thật theo `id`, từ chối lần thứ 4 bằng `ModelRetry` với **đúng câu cũ** ("segment N has used its 3 rewrites: accept it at 1.15 with the text you have"). Lời gọi lồng trong `run_code` vẫn đi qua hook này vì CodeMode dựng ToolManager lồng với `root_capability` của agent. Trong sandbox, lời từ chối hiện ra như một exception mà code bắt được.

Tôi đã cân nhắc ba chỗ đặt bộ đếm mà đề bài gợi ý, và chọn chỗ thứ tư:

- **Trong ports (bộ nhớ worker):** mất khi worker restart, và không chung giữa các worker; loại.
- **Trong store (DB), đếm từ activity như spike `--guard tool`:** bền qua restart, nhưng một attempt bị retry sẽ đếm hai lần trừ khi khoá theo activity id; các lời gọi gather cho cùng segment chạy song song trên nhiều worker cần khoá chống race; còn cần bảng mới và migration, và phải biết "một lần chạy Dubber" là gì khi workflow chạy lại Dubber ở vòng review sau. Nặng và dễ sai.
- **Trong deps:** deps là bất biến, được serialize vào mỗi activity; không đếm được.
- **Chọn: trạng thái của run, ở phía workflow.** `SpokenRewrites.for_run` trả một instance mới cho mỗi lần `agent.run`, nên mỗi lần chạy Dubber (kể cả vòng review sau) có bộ đếm riêng. Hook chạy trong code workflow (nơi quyết định gọi tool), không chạy trong activity. Vì thế:
  - **Restart worker:** workflow được replay từ history, các lời gọi được dispatch lại theo đúng thứ tự, bộ đếm được dựng lại y hệt; nếu bộ đếm lệch, replay sẽ phát sinh lệnh khác và Temporal báo NondeterminismError. Mỗi test guard trên Temporal đều replay lại chính history của nó bằng `Replayer` (đúng việc một worker mới làm) và đều qua.
  - **Activity retry:** workflow không thấy attempt; một lời gọi được đếm một lần dù chạy bao nhiêu attempt. Test: attempt đầu của lần nói thứ nhất ném lỗi kết nối, Temporal retry (history có attempt 2), `speak` bị gọi 4 lần (1 hỏng + 3 thành công), vẫn chỉ có 3 activity `tts`, và lần thứ 4 do code gọi bị từ chối, tức attempt hỏng không bị đếm.
  - **Gather cùng segment:** kiểm tra và tăng bộ đếm không có `await` ở giữa, nên 5 lời gọi gather cho một segment không thể cùng lọt qua; test cho thấy đúng 3 lần nói, 2 lần từ chối.
  - Lời gọi bị từ chối không thành activity và không tăng bộ đếm. Bộ đếm cộng dồn qua nhiều snippet trong cùng một run.

Đã kiểm chứng guard không mù: tạm bỏ dòng từ chối thì 4 test guard trên Temporal chuyển đỏ; khôi phục thì xanh.

## 3. Patch id và đường quyết định chế độ

- Patch mới: **`dubber-code-mode`** (`DUBBER_CODE_PATCH`).
- Run mới ghi marker này rồi gọi activity `dubber_setup`, trả `DubberSetup(mode, context)`; `context` chỉ có (và bắt buộc có) khi `mode="single"`, validator của contract chặn mọi tổ hợp khác. Chế độ được đọc ở worker từ settings và ghi vào history qua kết quả activity; code workflow không đọc env.
- Run ghi trước patch: đi đường cũ, `patched("dubber-single-request")` rồi `dubber_context`, hoặc không hỏi gì và chạy tool loop. Worker đặt `code` trả lời `dubber_context` là `None`, nên run cũ đang bay sẽ chạy tool loop chứ không chạy code (tôi chọn vậy vì `dubber_context` chỉ biết hai chế độ, và `tools` là gần nhất).
- Kiểm chứng gate có tác dụng: tạm gọi `dubber_setup` vô điều kiện thì cả 10 history cũ có đi qua Dubber đều replay hỏng (NondeterminismError), cộng thêm 4 history mới; khôi phục thì tất cả replay được. Tôi không thêm một workflow "ungated" cố định cho patch này như các patch khác, vì `run_dubber` là hàm module được workflow import thẳng, không có method để lớp con ghi đè (sửa `workflows/video_localization.py` nằm ngoài phạm vi file được phép).

## 4. Usage limits

- `tools`: giữ nguyên `dubber_usage_limits`.
- `code`: `dubber_code_usage_limits(lines)`:
  - `tool_calls_limit = 2 × CALLS_PER_LINE × lines + 40` (CALLS_PER_LINE = 1 đọc + 3 lần nói + 1 accept = 5). Theo mã nguồn harness, mỗi lời gọi lồng được tính vào `tool_calls_limit` khi nó thành công (lời gọi bị từ chối không tính), và bản thân mỗi `run_code` cũng tính. Gấp đôi trường hợp xấu nhất của mỗi dòng chừa chỗ cho `estimate_duration` và cho các lần đọc lại khi một snippet hỏng phải chạy lại; 40 dành cho các lời gọi `run_code`.
  - `request_limit = CALLS_PER_LINE × lines + 20`. Model gather đúng cách chỉ cần vài request cho mọi dòng (đọc, ≤3 vòng nói, accept, trả lời); nhưng một model không gather, mỗi turn một lời gọi một dòng, cần một request cho mỗi lời gọi, nên trần bằng trường hợp đó, cộng 20 cho câu trả lời và các lần retry của snippet/output.
  - `CodeMode(max_tool_calls=CODE_CALLS_PER_SNIPPET)` với `CODE_CALLS_PER_SNIPPET = CALLS_PER_LINE × MAX_SEGMENTS = 750`: một snippet có thể làm trọn một lượt cho mọi dòng mà Dubber có thể nhận (≤ 150, giới hạn của Localizer). Mặc định 100 sẽ chặn một snippet đọc và nói 60 dòng. Giới hạn ràng buộc thật là `tool_calls_limit` của run, tính theo số dòng thực.

## 5. Xử lý lỗi

- **Code lỗi cú pháp / lỗi khi chạy:** CodeMode biến thành `ModelRetry` gửi lại model (với sandbox mới, lỗi cú pháp được bộ kiểm kiểu của Monty báo dưới dạng "Type error in code … invalid-syntax"). Sau 3 lần retry, lần thứ 4 thành `UnexpectedModelBehavior` và đi đúng `_gave_up("Dubber", …)`: run kết thúc với `ApplicationError` non-retryable, type `UnexpectedModelBehavior`, thông điệp "the Dubber gave up". Đã test cả hai loại lỗi.
- **Vượt `tool_calls_limit`:** `UsageLimitExceeded` được harness ném lại sau snippet kể cả khi code bắt nó, và đi `_gave_up` với type `UsageLimitExceeded`. Đã test.
- **Lỗi giữa `gather`:** code gather `synthesize(0)`, `synthesize(1)`, `synthesize(99)` (99 không thuộc danh sách); lời từ chối không được bắt nên kết thúc snippet đó, model nhận lỗi kèm danh sách lời gọi đã bắt đầu, snippet sau làm nốt và run hoàn tất. Hai lần nói của snippet đầu đã thực sự diễn ra và được tính vào 3 lần của mỗi dòng. (Đã quan sát: các lời gọi anh em không bị huỷ, chúng chạy xong.) `ModelRetry` ném trong activity về workflow là `ModelRetry`, không phải `ActivityError`.
- **Bổ sung cần thiết mà đề bài không nêu: activity hỏng hẳn.** Trong `tools`, một activity hỏng hẳn (ví dụ `ArtifactMissingError` khi script mất, hoặc TTS hết retry) làm `agent.run` ném `ActivityError` và workflow chạy lại stage thích hợp. Trong `code`, lỗi đó lại thành exception trong sandbox mà code có thể bắt, hoặc thành retry cho model, nên khôi phục artifact sẽ không bao giờ xảy ra. `ActivityFailureEndsRun` giữ `ActivityError` đầu tiên của một lời gọi lồng và ném lại khi `run_code` kết thúc, bất kể code đã làm gì với nó. Test: code bắt lỗi `get_segment` (script mất) nhưng workflow vẫn chạy lại Localizer (`assemble_script` ×2, `dubber_setup` ×2). Tạm bỏ capability thì test này đỏ. Sửa cuối cùng, sau lần chạy toàn bộ: wrapper chỉ thay kết quả của `run_code` khi gặp `Exception`, không phải `BaseException`, để việc huỷ workflow (cancellation) vẫn đi qua nguyên vẹn.

## 6. Lệnh test và kết quả

Chạy trong `services/localization`:

| Lệnh | Kết quả |
|---|---|
| `uv run pytest -q tests/agents/test_dubber_code.py` | 19 passed |
| `uv run pytest -q tests/workflows/test_dubber_code_mode.py` | 12 passed |
| `uv run pytest -q tests/activities/test_agent_context.py tests/workers/test_agent_wiring.py` | 43 passed |
| `uv run pytest -q tests/replay` | 272 passed |
| `uv run pytest -q tests/agents tests/activities/test_agent_context.py tests/workers tests/replay/test_replay.py` (trước khi viết test mới, để bắt hồi quy) | 612 passed, 2 deselected |
| `uv run ruff check .` và `uv run ruff format --check .` | sạch |
| `uv run pytest -q` (toàn bộ) | 2981 passed, 7 deselected (marker `live`), 1 warning có sẵn từ trước, 0 failed |
| Sau sửa cuối: `uv run pytest -q tests/agents/test_dubber_code.py tests/agents/test_dubber.py tests/workflows/test_dubber_code_mode.py tests/activities/test_agent_context.py tests/workers/test_agent_wiring.py tests/replay` | 376 passed |

Ghi lại history mới: `PYTHONPATH=. uv run python -m tests.replay.record_histories dubber-code-mode` (Temporal dev server cục bộ do test tự khởi, không đụng docker hay dữ liệu thật).

Không chạy mutation harness, không chạy video thật, không đụng docker/Temporal/Postgres thật, không đổi dữ liệu nào.

## 7. Đã kiểm chứng và chưa kiểm chứng

Đã kiểm chứng (model kịch bản, sandbox Monty thật, Temporal test server thật):

- Mỗi `synthesize` lồng là một activity riêng trên queue `tts`, `get_segment` trên `io`, `accept` chạy inline; chỉ `run_code` được đưa cho model.
- Các lần nói được gather chạy chồng nhau thật: overlap đo được bằng 3 với 3 dòng; đối chứng với `tts` limit = 1 cho overlap 1.
- Guard qua gather, qua nhiều snippet, qua activity retry, và replay lại được.
- Các đường lỗi ở mục 5, và replay của mọi history cũ lẫn mới.

Chưa kiểm chứng:

- **LLM thật (`gpt-6-luna`) có viết được code Monty hợp lệ, có gather, có bắt lỗi đúng cách không.** Không đo được ở đây, không giả lập.
- Chất lượng quyết định so với `single`/`tools` trên video thật (7616, 7618).
- Kill worker giữa chừng trên server thật cho chế độ `code` trong service (spike đã làm trên compose; ở đây tôi chỉ chứng minh qua replay từ history).
- Hành vi khi conversation của `dubber_code` rất dài: metadata của `run_code` chứa toàn bộ lời gọi lồng; Dubber không có `ConversationSizeGuard` như Localizer.

## 8. Rủi ro

- Code của model chạy trong code workflow: Temporal yêu cầu một workflow task trả quyền trong khoảng 2 giây, nên code nặng (vòng lặp tính toán lớn) có thể bị deadlock detector của Temporal bắt. Prompt dặn giữ code ngắn và nhẹ; `max_duration_secs` bị harness tắt trong workflow.
- Gather nhiều dòng làm nhiều activity `tts` cùng xếp hàng; chúng chờ slot theo `TTS_CONCURRENCY`, có `start_to_close` 5 phút sau khi bắt đầu nhưng không có `schedule_to_start`, nên một hàng đợi rất dài chỉ làm chậm chứ không hỏng.
- Harness kéo theo một số gói không dùng tới (`ddgs`, `lxml`, `primp`…), làm image lớn hơn.
- Run đang bay lúc deploy với worker đặt `code` sẽ chạy tool loop (xem mục 3), không phải code.

## Câu hỏi còn mở

1. Có chạy so sánh với LLM thật (`DUBBER_MODE=code`) trên 7616/7618 trước khi đổi mặc định không? Bước này cần khoá OpenRouter và chạy video thật, nên cần anh/chị cho phép.
2. Có muốn thêm `ConversationSizeGuard` cho Dubber ở chế độ `code` không (metadata `run_code` làm conversation lớn nhanh hơn `tools`)?
3. Có chấp nhận các gói phụ mà `pydantic-ai-harness` kéo vào image không, hay muốn tìm cách cài gọn hơn?

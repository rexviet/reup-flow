# Spike: Dubber gọi `synthesize` qua CodeMode của Pydantic AI

Kết luận: **CodeMode chạy được cùng Temporal và giữ được các tính chất ta cần**, với một ngoại lệ phải sửa: guard "tối đa 3 lần synthesize mỗi segment" hiện tại bị mù khi các lời gọi nằm trong code do model viết.

Spike: `services/localization/spikes/g7_code_mode.py`. Chạy lại bằng `./run.sh uv run python g7_code_mode.py kill` và `... guard --guard history|tool`. Model là `FunctionModel` có kịch bản sẵn, nên chỉ đo framework, không đo chất lượng code do LLM thật viết.

## Kết quả

| Câu hỏi | Kết quả | Bằng chứng |
|---|---|---|
| Q1. Mỗi lời gọi tool lồng trong code có là một Temporal activity, đúng queue TTS không? | Có | 4 activity `call_tool` trên queue `g7-tts`, 2 activity `model_request` trên queue `g7-llm` |
| Q2. `asyncio.gather` trong sandbox có chạy chồng nhau thật không? | Có | 3 lời gọi cùng bắt đầu ≈ 18.37 s, cùng kết thúc ≈ 19.88 s, mỗi lời gọi 1.51 s; overlap tối đa = 3 |
| Q3. Sau SIGKILL worker, lời gọi đã xong có chạy lại không? | Không | Segment 0, 1, 2 mỗi segment chạy đúng 1 lần; workflow tiếp tục ở process mới |
| Q3'. Lời gọi bị giết giữa chừng thì sao? | Chạy lại đúng 1 lần | Segment 3 có 2 lần thực thi: ngữ nghĩa at-least-once bình thường của Temporal |
| Q4a. Guard đếm `ToolCallPart` trong history (`RewriteLimit` hiện tại) còn tác dụng không? | **Không** | Gọi 5 lần cho cùng một segment, cả 5 lần đều chạy TTS |
| Q4b. Guard đặt trong tool (đếm ở tầng activity/ports) thì sao? | Có | Chỉ 3 lần chạy; lần 4 và 5 bị từ chối, `ModelRetry` hiện ra trong code dưới dạng exception mà code bắt được |

Một sai sót của chính spike đã được sửa trong lúc làm: lần chạy đầu báo overlap = 2 vì hàm đo đọc nhầm cột. Timeline thật cho thấy overlap là 3, và kịch bản kill ban đầu kill *trước* khi lời gọi chậm bắt đầu nên chưa kiểm được trường hợp giữa chừng. Cả hai đã sửa và chạy lại.

## Điều cần làm nếu chuyển Dubber sang CodeMode

1. Chuyển guard `RewriteLimit` (dubber.py dòng 196–219) vào tầng tool hoặc `DubberPorts`, đếm bằng trạng thái thật (ví dụ đếm lần `speak` theo segment) thay vì đọc history. Đây là thay đổi bắt buộc, không phải tuỳ chọn.
2. Thêm `workflow.patched` và history replay mới: dạng history của Dubber thay đổi (có thêm lời gọi `run_code`).
3. Viết lại phần instructions: nói cho model biết có `run_code`, tool nào gọi được trong sandbox, và rằng `ModelRetry` từ tool sẽ thành exception trong code.
4. `usage_limits` (`dubber_usage_limits`) cần xem lại: theo tài liệu, lời gọi lồng vẫn tính vào `tool_calls_limit`, và `CodeMode` có `max_tool_calls` mặc định 100 mỗi `run_code`.

## Chưa kiểm, cần làm trước khi quyết định

- **LLM thật có viết được code Monty đúng không.** Monty là tập con của Python (chỉ có `asyncio`, `json`, `re`, `math`...). Cần chạy `gpt-6-luna` (Dubber hiện dùng) với `CodeMode` trên một batch thật và đo tỉ lệ code lỗi cú pháp hoặc lạm dụng tính năng không hỗ trợ.
- **Chất lượng quyết định** có tốt hơn, bằng, hay kém so với gọi tool từng cái. Cần so với 7616 và 7618.
- **Giới hạn của tài liệu:** workflow task phải trả quyền điều khiển trong 2 giây, nên code trong sandbox không được tính toán nặng; `max_duration_secs` bị tắt trong workflow.
- **Phiên bản:** spike resolve ra `pydantic-ai-slim 2.63.0`, `pydantic-ai-harness 0.63.0`, `pydantic-monty 1.4.0`, trong khi service đang pin `pydantic-ai-slim==2.61.0`. Phải kiểm harness có chạy trên 2.61.0 không, hoặc nâng pin.
- **Replay thật:** chưa chạy `replay` trên history có `run_code` (ngoài quy ước của dự án).

## Thay đổi để lại trong repo (chưa commit)

- `services/localization/spikes/g7_code_mode.py` (mới)
- `services/localization/spikes/pyproject.toml` và `uv.lock` (thêm `pydantic-ai-harness[codemode,temporal]`)

## Câu hỏi còn mở

- Có chạy bước tiếp theo (LLM thật với CodeMode trên một batch thật của 7616 hoặc 7618) không? Bước này cần máy GPU hoặc ít nhất khoá OpenRouter.

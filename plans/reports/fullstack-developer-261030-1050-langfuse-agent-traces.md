# Trace Langfuse đọc được cho các agent (Dubber `code`, Localizer, Reviewer)

Ngày 2026-10-30, nhánh `feat/localization-dubber-code-mode`. Chưa commit.

## Kết quả

Trace của một run giờ là một cây gọn. Mỗi agent có một node `invoke_agent` mang input và
output. Bên dưới là từng model request (`chat <model>`) với toàn bộ messages vào, câu trả
lời ra, token và cost thật OpenRouter báo. Với Dubber ở chế độ `code`, `run_code` có input
là đoạn code model viết và output là giá trị code trả về. Dưới `run_code` là từng tool mà
code đã gọi (`get_segment`, `synthesize`, `accept`), mỗi cái có arguments và result.
Không còn span `StartActivity:*`, `CompleteWorkflow:*`, `StartWorkflow:*`, và không còn
`RunActivity:agent__*`.

Mỗi model request là đúng một observation mang token và cost (span `chat`). Cost lấy từ
body OpenRouter, không ước tính. Đặt `TRACE_AGENT_CONTENT=false` thì không còn prompt,
output, code hay arguments trong span nào, còn token và cost vẫn giữ. Bộ test của service:
xem mục "Kiểm chứng".

Cây đọc lại từ Langfuse thật qua public API, sau một lần SIGKILL worker giữa lúc code đang
gọi `synthesize` (cột `in/out` cho biết Langfuse có input, output hay không):

```
RunWorkflow:VideoLocalizationWorkflow  [SPAN] --/---
  RunActivity:ingest / voice_rate / detect_subtitles / transcribe / localizer_context  [SPAN]
  localizer run  [AGENT] in/out
    chat localizer-model  [GENERATION] in/out  tokens=66 cost=None
    read_transcript  [TOOL] --/out
    chat localizer-model  [GENERATION] in/out  tokens=111 cost=None
  RunActivity:assemble_script / voice_profile / plan_synthesis / synthesize_segments x2 / measure_fit / dubber_setup  [SPAN]
  dubber_code run  [AGENT] in/out
    chat dubber-model  [GENERATION] in/out  tokens=102 cost=0.00013
    run_code  [TOOL] in/out
      get_segment  [TOOL] in/out
      get_segment  [TOOL] in/out
      synthesize  [TOOL] in/out
      synthesize  [TOOL] in/out
      accept  [TOOL] in/out
      accept  [TOOL] in/out
    chat dubber-model  [GENERATION] in/out  tokens=161 cost=0.00013
  RunActivity:write_dubbed_script / plan_synthesis / synthesize_segments x2 / commit_dub / assemble_audio / layout_captions / review_before_render / render / publish  [SPAN]
```

Các dòng `RunActivity` được gộp lại cho gọn. Bản đầy đủ có 37 node, nằm trong
`services/localization/spikes/out/g9-service.json`. Localizer trong lần chạy này dùng model
giả không qua HTTP nên cost là `None`. Hai request của Dubber thì có cost do stub trả về
qua đúng client production.

Nội dung đọc lại từ Langfuse: input của `run_code` là
`{"code": "import asyncio\nids = [0, 1]\nshown = await asyncio.gather(...)..."}`, output là
`{"return_value": ["speaking takes 900 ms for a 1000 ms slot ...", ...]}`. Input của
`synthesize` là `{"id": 1, "text_vi": "câu ngắn 1"}`. Input của `chat dubber-model` bắt đầu
bằng system prompt của Dubber, và output là tool call `final_result`.

## Trước và sau

Cùng một run (Dubber `code`, 2 dòng), đo trong bộ nhớ với cấu hình cũ (TracingInterceptor
của Temporal, không bật instrumentation của pydantic-ai):

| | Trước | Sau |
|---|---|---|
| Tổng số span của run | 65 | 37 |
| Phần của Dubber | 12 span phẳng: 2 cặp `StartActivity`/`RunActivity:agent__dubber_code__model_request` và 4 cặp `...toolset__<agent>__call_tool` | 10 node lồng nhau: agent, 2 chat, `run_code`, 6 tool |
| Nội dung (prompt, code, args) | không có | có ở mọi node của agent |
| `accept` (tool thuần, chạy inline) | không có span | có span |

Trước đây không thể phân biệt `get_segment` với `synthesize`, vì cả hai đều là
`call_tool`. Gốc cũ là `StartWorkflow`, kèm một `RunWorkflow` dài 0 ms và một
`CompleteWorkflow`.

## Đã đổi những gì

Tất cả đường dẫn đều nằm dưới `services/localization/` (trừ hai file ở gốc repo, có ghi rõ).

Mới:
- `src/localization/observability/temporal_tracing.py`: `RunTracingInterceptor` thay cho
  `TracingInterceptor` của Temporal. Nó chuyển trace context qua header `_tracer-data`
  (cùng định dạng cũ). Nó chỉ tạo hai loại span: `RunWorkflow:<type>` làm gốc (mở trong
  workflow bằng tracer replay-safe, mang `session.id` = aweme id), và
  `RunActivity:<type>` cho các activity là bước của run. Activity `agent__*` không có span.
- `src/localization/observability/agent_tracing.py`: `agent_instrumentation()` (không bao
  giờ có binary content, theo công tắc content) và `instrument_agents()`
  (`Agent.instrument_all`).
- `src/localization/observability/generation.py`: `LlmUsage`, `usage_attributes` và tên các
  attribute Langfuse. Chuyển từ `llm_usage.py` sang, thuần không I/O, để code workflow
  import được (`llm_usage` vẫn re-export).
- `src/localization/observability/model_cost.py` (phía workflow): capability
  `CostOnRequestSpan` ghi cost và token lên span `chat`, cùng `with_reported_cost`.
- `src/localization/observability/reported_cost.py` (phía worker): `ReportedCostModel` bọc
  model đã bind. Nó gom usage mà response hook đọc được trong request, rồi đặt cost vào
  `ModelResponse.usage.cost` và phần chi tiết vào `provider_details`.
- `src/localization/observability/span_redaction.py`: `RedactingExporter` thay mọi
  `SecretStr` của Settings và mọi giá trị `Bearer|Basic <token>` bằng `[REDACTED]` trước
  khi export.
- `tests/observability/test_agent_traces.py` (8 test), `tests/observability/test_span_redaction.py` (5 test).

Sửa:
- `src/localization/observability/tracing.py`: thêm `WorkflowSafeTracerProvider` (provider
  SDK, tracer là replay-safe của Temporal qua `create_tracer_provider`, dùng chung
  processor). Exporter đi qua `RedactingExporter`. `tracing_interceptor()` trả về
  interceptor mới. Docstring cũ ("Nothing here turns on pydantic-ai's own instrumentation")
  đã viết lại.
- `src/localization/observability/llm_usage.py`: hook đẩy usage vào `collect_usage()`.
  Docstring cũ ("never a prompt") đã viết lại.
- `src/localization/agents/temporal.py`: thêm `CostOnRequestSpan()` vào capabilities của mọi
  durable agent (localizer, dubber, dubber_code, các agent single/repair, slot shortener).
- `src/localization/agents/reviewer.py`: dùng `agent_instrumentation(tracer_provider)`, nên
  Reviewer theo cùng công tắc và vẫn không có frame.
- `src/localization/config.py`: `trace_agent_content: bool = True` (`TRACE_AGENT_CONTENT`).
- `src/localization/workers/run.py`: `init_tracing`, rồi `instrument_agents(provider,
  include_content=settings.trace_agent_content)` khi Langfuse được cấu hình. Model của
  Localizer và Dubber được bọc bằng `ReportedCostModel`.
- `.env.example` và `docker-compose.yml` ở gốc repo: thêm `TRACE_AGENT_CONTENT` (chỉ cho
  `worker-agents`).
- Test: `tests/test_tracing.py` (đổi test interceptor sang `RunTracingInterceptor`, thêm 7
  test), `tests/observability/test_llm_cost.py` (+6 test về cost đi qua response và lên
  span), `tests/observability/test_trace_completeness.py` (bước "localizer model request"
  giờ là span `chat` của Localizer, chạy với agent được instrument như worker),
  `tests/support/spans.py` (provider toàn cục là `WorkflowSafeTracerProvider`, thêm
  `instrumented_agents()`).
- `tests/mutations/phase-08.sh` và `phase-09.sh`: mutation "frames in the telemetry"
  giờ trỏ vào `agent_tracing.py`. Hai mutation về attribute generation trỏ vào
  `generation.py`, vì đoạn code bị mutate đã chuyển sang đó. Không chạy mutation.
- `spikes/g9_instrumented_trace.py`: thêm chế độ `service`, chạy trên host với code
  production, Temporal dev server local, Langfuse thật, SIGKILL worker giữa chừng, rồi đọc
  trace về và in cây.

Không có test cũ nào khẳng định "không ghi prompt". Có hai test được đổi để theo hành vi
mới: test interceptor trong `tests/test_tracing.py` và matcher bước "localizer model
request" trong `test_trace_completeness.py`.

## Lựa chọn thiết kế

### Bỏ span nhiễu: sửa ngay chỗ interceptor tạo span, không lọc ở exporter

Tôi chọn cách bớt span ngay tại interceptor (viết interceptor riêng), không dùng một
exporter bỏ span rồi gắn lại cha cho span con. Lý do:
- Exporter chỉ gắn lại cha được nếu cha và con nằm trong cùng một process, và phải ghi lại
  cha từ lúc `on_start`. Nhưng `StartActivity` sinh ra ở worker chạy workflow, còn
  `RunActivity` sinh ra ở worker chạy activity. Sau một lần kill, workflow còn được replay ở
  process khác. Khi đó con sẽ trỏ tới một cha không bao giờ được export.
- Bớt span tại nguồn thì không cần ghi sổ gì cả. Context vẫn đi qua header, nên span của
  pydantic-ai trong workflow tự là cha của những gì xảy ra trong activity. Ví dụ, nếu
  `synthesize` gọi TTS thật thì `provider.tts` sẽ nằm dưới `execute_tool synthesize`.
- `OpenTelemetryInterceptor(add_temporal_spans=False)` mới của Temporal thì không dùng được:
  nó bỏ cả gốc của run, nên mỗi agent và mỗi activity thành một trace riêng. Nó cũng báo lỗi
  khi provider toàn cục không phải `ReplaySafeTracerProvider` của Temporal (trường hợp
  Langfuse tắt). Còn bật `add_temporal_spans=True` thì `StartActivity` quay lại.

Các span không có data còn lại, và lý do giữ:
- `RunWorkflow:<type>`: gốc của trace, mang `session.id`. Thiếu nó thì một run tách thành
  nhiều trace.
- `RunActivity:<type>` của các bước không phải agent (ingest, transcribe, publish...): đây
  là các bước của pipeline. `provider.stt/ocr/tts` và cost của request Jev/OCR nằm dưới
  chúng, và `tag_session` cũng ghi lên chúng. Chúng là anh em của node agent, không xen vào
  cây của agent.

### Đếm trùng cost: cost đi theo câu trả lời về span `chat`

Span `chat` được pydantic-ai tạo trong code workflow. HTTP request thì chạy trong activity,
nên hook không thể ghi cost lên span đó. Cách làm:
1. Ở activity, `ReportedCostModel` bọc model. Response hook vẫn đọc body như trước (ghi DB
   `llm_calls` như cũ), và còn đẩy `LlmUsage` vào một `ContextVar`. Model lấy usage có
   generation id khớp `provider_response_id`, rồi đặt `usage.cost` (Decimal) và
   `provider_details["openrouter_cost_details"]`.
2. `ModelResponse` đi về workflow qua payload Temporal. Đã có test round-trip qua
   pydantic payload converter.
3. Trong workflow, `CostOnRequestSpan` nằm bên trong capability `Instrumentation`
   (outermost), nên span hiện hành lúc đó chính là `chat`. Nó ghi
   `langfuse.observation.type=generation`, `usage_details` và `cost_details` lên span này.

Vì activity của agent không còn span, hook không tìm thấy span đang ghi nào, nên không có
observation thứ hai. `invoke_agent` mang `gen_ai.aggregated_usage.*`, nhưng Langfuse bỏ qua
usage và cost của span `invoke_agent` (đã đọc trong `OtelIngestionProcessor.ts`). Cũng từ
mã nguồn Langfuse: `operation.cost` (pydantic-ai tự ước tính qua genai-prices khi response
không có cost) không được Langfuse đọc. Langfuse chỉ đọc
`langfuse.observation.cost_details` > `gen_ai.usage.cost` > `llm.cost.total`. Khi OpenRouter
không báo cost thì span không có cost. Nhưng Langfuse có thể tự suy ra cost từ bảng giá
model của nó: đó là ước tính của Langfuse, giống hành vi trước đây.

Reviewer (agent không durable, chạy trong activity) giữ cách cũ. Span `chat` của nó đang là
span hiện hành lúc gửi HTTP, nên hook ghi cost thẳng lên đó, vẫn là một observation. OCR và
Jev cũng như cũ: cost nằm trên span `provider.ocr` hoặc `RunActivity:review_*`.

### Replay

`WorkflowSafeTracerProvider` trả về tracer replay-safe của Temporal. Trong workflow, span id
lấy từ random tất định của workflow, và span không được end khi đang replay. Những span mở
trước khi kill (`invoke_agent`, `run_code`, `synthesize` đang chạy) được mở lại với cùng id
và thời điểm bắt đầu cũ, và chỉ được end một lần ở worker hoàn tất. `workflow.new_random()`
không lấy số từ `workflow.random()`, nên không đổi hành vi workflow. Workflow cũng không dùng
random.

### Bí mật

Đã kiểm tra:
- `include_binary_content=False` cho mọi agent: frame và clip không vào span.
- Header và API key không nằm trong messages của pydantic-ai. Span `chat` của durable agent
  dùng `DeferredModel`, không có base URL.
- Rủi ro thật là một gateway trả về lỗi có chứa header Authorization (đã thử: key lọt vào
  span qua message của `ModelHTTPError` khi content bật). Vì vậy tôi thêm
  `RedactingExporter`, có test canary và test đối chứng.
- `model_request_parameters` chỉ chứa metadata tool (ActivityConfig, tên queue), không có
  bí mật.
- `vbee_voice_id` là `SecretStr` nên cũng được redact.

Giới hạn: giá trị bí mật ngắn hơn 8 ký tự không được thay.

## Kiểm chứng

- Test trong bộ nhớ (Temporal local server, workflow thật, Dubber `code` với model kịch
  bản, mỗi request có reply giá thật qua `openrouter_client` + stub):
  - Kiểm tra cây, nội dung, cost từng request, và việc không có span wrapper.
  - Với content tắt: không còn instructions, prompt, code, kết quả tool hay câu trả lời,
    còn cost thì vẫn đủ.
  - Replay 4 history `dubber-code-mode` khi tracing bật: export 0 span. Có đối chứng:
    provider không replay-safe thì export lại span.
  - Dừng toàn bộ worker giữa lúc `synthesize`, workflow vẫn `RUNNING`, worker mới chạy
    nốt: không span nào bị export hai lần, và cây agent vẫn đủ.
- Langfuse thật (`uv run python spikes/g9_instrumented_trace.py service`):
  - Worker 1 bị SIGKILL (exit -9) khi hai `synthesize` đang chạy (`speak` được gọi bởi pid
    52105 cho dòng 0 và 1, sau đó pid 56384 gọi lại cả hai).
  - Cây đọc về có 37 node, không có wrapper. `synthesize` vẫn là 2 node, không nhân đôi.
    `chat dubber-model` vẫn là 2 node, mỗi node có cost.
  - Chạy với `TRACE_AGENT_CONTENT=false`: cây giữ nguyên 37 node, node agent và tool không
    còn input/output chữ, `chat` chỉ còn cấu trúc (role, tên tool, định nghĩa tool), cost
    vẫn còn. Kết quả ở `spikes/out/g9-service-no-content.json`.
  - Chuỗi `asyncio.gather` vẫn xuất hiện, nhưng đó là mô tả tĩnh của tool `run_code` do
    CodeMode sinh ra, không phải code của run.
- Test của service: chạy hẹp trước (tracing, observability, architecture, reviewer,
  workers, replay, secret canary: 520 passed), rồi chạy toàn bộ: **3006 passed, 7
  deselected** (marker `live`), 0 failed, mất 607 s. Trong lần chạy toàn bộ đầu tiên có 4
  test kiến trúc đỏ, vì code workflow chạm tới `httpx` và các stage
  synthesize/layout/assemble chạm tới `pydantic_ai` qua `tracing.py`. Tôi sửa bằng cách tách
  module (`generation.py`, `agent_tracing.py`, `reported_cost.py`), không nới luật nào.
  `ruff check` và `ruff format --check` trên `src` và `tests` đều sạch.

Chưa kiểm chứng:
- Chưa chạy video thật nào (VoiceStudio đang tắt, và đây là ràng buộc của task).
- Chưa thấy cost thật của OpenRouter trên một model thật; cost trong lần chạy trên là của
  stub, đi qua đúng hook và client production.
- Trace của Reviewer chế độ `llm` chưa được chạy lại với Langfuse thật.
- Chưa chạy mutation (theo quy tắc của user).

## Rủi ro

- Span của những việc worker đã làm xong nhưng chưa flush trước khi bị kill (batch mỗi
  500 ms) sẽ mất. Ví dụ: `provider.tts` của lần thử bị kill, hoặc một tool đã xong ngay
  trước khi kill. Chấp nhận được.
- `create_tracer_provider` và replay-safe tracer bị Temporal đánh dấu là experimental. Nếu
  API đổi khi nâng temporalio, `WorkflowSafeTracerProvider` sẽ hỏng.
- Kích thước trace: mỗi span `chat` mang toàn bộ hội thoại tới lúc đó. Ở chế độ `tools`,
  Dubber có thể gửi tới `10*lines+20` request, nên trace có thể lên nhiều MB. Batch
  exporter hay Langfuse có thể từ chối span quá lớn. Chưa đo với video dài.
- Prompt chứa chữ của video (dữ liệu không tin cậy) nay nằm trong Langfuse local. User đã
  đồng ý, và có công tắc để tắt.
- Một run đang chạy dở lúc deploy sẽ có trace lai: phần trước có span kiểu cũ, phần sau có
  gốc `RunWorkflow` mới, được export khi run kết thúc.
- Gốc `RunWorkflow` chỉ được export khi run kết thúc. Trong lúc run đang chạy, Langfuse
  hiện các node con mà chưa có gốc.

## Câu hỏi còn mở

1. Docstring của `eval/run_experiment.py` (`SpanPerRequest`) vẫn ghi "as the model-request
   activity's span does in production". Câu này giờ sai, nhưng file không nằm trong phạm vi
   được sửa. Eval vẫn chạy được (`init_tracing` vẫn trả về một `TracerProvider` của SDK).
   Có sửa câu đó không?
2. Có nên đặt `include_model_request_parameters=False` để span `chat` nhỏ hơn không (bỏ
   schema và metadata của tool, chỉ giữ `gen_ai.tool.definitions`)? Hiện tại đang để mặc
   định là bật.
3. Năm target mutation cũ trong `phase-04.sh` và `phase-08.sh` (DETECT, PUBLISH,
   REVIEWACT, WORKFLOW, RUN) đã không còn khớp với mã nguồn từ trước thay đổi này. Cần sửa
   trước lần chạy mutation sau phase 10.
4. Trong lúc làm có thay đổi song song không phải của tôi
   (`src/localization/activities/layout_captions.py`,
   `tests/activities/test_layout_captions.py`, `eval/*_spike.py` mới). Tôi không đụng tới
   các file đó.

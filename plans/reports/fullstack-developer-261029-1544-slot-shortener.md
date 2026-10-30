# Rút gọn từng slot quá dài (phase 3)

Ngày 2026-10-29. Kế hoạch: `plans/261029-1544-vbee-dubbing-pipeline/plan.md`.

Trạng thái: xong. Phase này không sửa workflow, không đổi contract `vbee_flow.py`.

## Phase 4 cần gọi gì

```python
from localization.agents.temporal import (
    run_slot_shortener,          # async (plan: SlotPlan, syllables_per_sec: float) -> SlotPlan
    slot_shortener_agent,
    slot_shortener_repair_agent,
)
plan = await run_slot_shortener(plan, rate)   # gọi trong workflow code
```

Phase 4 phải đăng ký hai agent ở hai chỗ, giống bốn agent one-request hiện có:

- Thêm `slot_shortener_agent` và `slot_shortener_repair_agent` vào `__pydantic_ai_agents__` của `VbeeDubbingWorkflow`.
- Thêm `AgentPlugin(...)` của hai agent vào danh sách `agents` của queue `llm` trong `workers/run.py`.

Không cần `workflow.patched`, vì đây là workflow type mới và chưa có history cũ.

Model dùng alias `MODEL_ALIAS`, tức model của Localizer. Worker đã bind alias này qua `bind_model_factory`, bọc trong `AnswerOnlyModel` theo `LOCALIZER_REASONING`. Mỗi request mang `answer_only_settings(số slot được hỏi)`, và mỗi lần `run` giới hạn `request_limit=2`. Các model request được ghi lại thành activity `agent__slot_shortener__model_request` và `agent__slot_shortener_repair__model_request`.

`run_slot_shortener` hoạt động như sau:

- Báo lỗi `ApplicationError` non-retryable khi rate ≤ 0. Lỗi thường trong workflow code sẽ bị retry mãi, nên phải đổi sang loại này.
- Báo lỗi `PayloadTooLarge` khi prompt lớn hơn `MAX_CONVERSATION_BYTES // 2`.
- Ngoài hai trường hợp đó, hàm gọi `slot_shortener.shorten`.

Phần logic thuần nằm trong `src/localization/agents/slot_shortener.py`, gồm `shorten`, `overflowing`, `problem`, `render_prompt`, `render_repair_prompt`, `build_agent` và `settings_for`. Script offline cũng gọi đúng `shorten` này.

## Luồng xử lý

1. Code chọn các slot có `estimate_ms(text, rate) > slot_ms * STRETCH_MAX`. Nếu không có slot nào thì trả plan nguyên vẹn và không gọi LLM.
2. Gửi một request chỉ chứa những slot đó.
3. Code kiểm từng câu trả lời. Câu nào sai thì gửi một request repair, chỉ mang các câu sai kèm lý do.
4. Câu vẫn sai sau repair thì giữ chữ gốc, đưa vào `unfit_ids` và ghi note `slot N kept its text: <lý do>` (cắt ở 500 ký tự).
5. Nếu model không trả được câu trả lời dùng được (`UnexpectedModelBehavior`, `UsageLimitExceeded`), mọi câu đã hỏi được xử lý như câu sai. Video vẫn được lồng tiếng với chữ đầy đủ, chứ không fail cả run.
6. Plan trả về chỉ đổi `text` của các slot đã rút gọn; mốc thời gian giữ nguyên. Các `shortened_ids`, `unfit_ids`, `notes` có sẵn trên plan đầu vào được giữ lại và gộp với kết quả mới.

## Prompt

Instructions (trích): mô tả nhiệm vụ, `DATA_RULE` về khối `<untrusted_video_text>`, và hai câu ngữ cảnh chỉ để đọc (không được viết lại, không được dời chữ giữa các câu). Các quy tắc cụ thể:

- Chỉ cắt bằng đúng mức budget cần, đừng cắt quá sâu.
- Giữ nghĩa, giữ số ở dạng chữ số, giữ tên, thương hiệu, từ nước ngoài và lời kêu gọi hành động.
- Bỏ từ đệm, phần lặp lại và từ làm mềm câu trước, sau đó mới rút gọn cách diễn đạt.
- Không thêm URL, email hay số điện thoại.
- Trả về đúng một dòng `(id, text)` cho mỗi id được hỏi.

Mỗi slot trong prompt có dạng sau. Header do code tạo nên nằm ngoài khối, còn chữ lấy từ video nằm trong khối và được escape:

```
Shorten these lines so they fit their slots. The slot ids are [6, 9, 11, 13].

[6] slot 7129 ms, up to 36 syllables; the line has 41 now
<untrusted_video_text>
before (context only, do not rewrite): <câu slot 5>
line: <câu slot 6>
after (context only, do not rewrite): <câu slot 7>
</untrusted_video_text>
```

Prompt repair có dạng `[id] slot …, up to N syllables`, rồi khối untrusted chứa `original:` và `your line:`, rồi dòng `Problem: …`. Output có cấu trúc `ShortLines{lines: [{id, text}]}`.

## Code kiểm câu trả lời (`problem`)

Thứ tự kiểm:

1. Id phải là id đã hỏi. Câu trả lời cho id khác bị bỏ qua. Nếu một id được trả nhiều lần thì lấy lần đầu. Repair chỉ được ghi đè các id nó được hỏi.
2. Câu không được rỗng, tức phải có ít nhất 2 ký tự chữ hoặc số (giống `line_findings`).
3. Không có ký tự mà font phụ đề không hiển thị được (`foreign_chars`).
4. Phải giữ mọi số và mọi từ nước ngoài của câu gốc.
   - Số được kiểm bằng `review_checks._digits_kept`, nên số viết thành chữ cũng được tính là giữ.
   - Từ nước ngoài là token ASCII mà `vi_syllables._is_foreign` coi là ngoại. Nhờ vậy "mua", "cho", "anh" không bị bắt giữ.
   - Không dùng thẳng `line_findings`: regex Latin của hàm đó áp lên câu tiếng Việt sẽ bắt cả "xin", "ch"… và chặn mọi lần rút gọn.
5. Câu phải vừa slot: `estimate_ms ≤ slot_ms × 1.15`. Budget đưa cho model là `syllable_budget(slot_ms, rate)`, tức số âm tiết ở tốc độ 1.0×.

## Test (TDD, viết test trước)

- `tests/agents/test_slot_shortener.py`: 21 test, dùng `FunctionModel`.
  - Không slot nào tràn thì không gọi LLM; plan không có lời cũng vậy.
  - Rate ≤ 0 bị từ chối.
  - Chỉ slot tràn được gửi, kèm budget.
  - Câu hàng xóm chỉ là ngữ cảnh; slot đầu và slot cuối ghi rõ không có hàng xóm.
  - Khối untrusted escape được chuỗi tấn công.
  - Settings là answer-only.
  - Câu trả lời tốt được áp dụng và thời gian giữ nguyên. Số viết bằng chữ được chấp nhận.
  - Mất số dẫn đến repair; mất từ Latin dẫn đến repair; sai hai lần dẫn đến `unfit` kèm note.
  - Câu vẫn quá dài dẫn đến repair rồi `unfit`.
  - Sai id dẫn đến repair. Câu rỗng hoặc có chữ Hán dẫn đến repair.
  - Câu đã đạt được giữ khi câu khác đi repair, và repair không ghi đè được câu không được hỏi.
  - Câu trả lời không parse được dẫn đến `unfit`. Trạng thái của lượt trước được giữ.
- `tests/workflows/test_slot_shortener_flow.py`: 3 test trên Temporal thật, có workflow phụ trong `tests/support/slot_shortener_flow.py`.
  - Repair một lần thì có đúng hai activity model request, mỗi agent một activity, và cả hai mang answer-only settings.
  - Không có slot tràn thì không có activity nào.
  - Rate 0 làm workflow fail thay vì bị retry mãi.
- Mutation thủ công trên 6 guard: bỏ lọc id, bỏ kiểm số/Latin, bỏ kiểm độ dài, bỏ bọc untrusted, gửi mọi slot, áp dụng cả câu sai. Cả 6 đều cho test đỏ. Guard lọc id ban đầu vẫn xanh, nên tôi đã thêm test repair ghi đè id không được hỏi.
- Lệnh `uv run pytest -q tests/agents tests/workflows/test_one_request_agents.py tests/workflows/test_slot_shortener_flow.py tests/replay`: 522 passed.
- `ruff check` và `ruff format --check` sạch trên các file của tôi. `ruff check .` báo một lỗi F841 ở `activities/plan_slots.py`, là file của agent phase 2 đang làm, không phải file của tôi.

## Chạy thật offline (7618 v2)

Tổng cộng 2 LLM call, $0,000413. Model `openai/gpt-6-luna` qua OpenRouter, reasoning off, provider OpenAI.

- Ở 6 âm tiết/s không slot nào tràn: tỉ lệ cao nhất là 1,02, ngưỡng là 1,15. Vì vậy tôi hạ rate xuống **5,0 âm tiết/s**, khi đó 4 slot tràn: 6, 9, 11, 13.
- **Lần 1** (prompt cũ, "at or under"): 1 request, 3,8 s, $0,000201. Cả 4 slot đạt, nhưng model cắt quá sâu. Ví dụ slot 9 xuống còn 4,6 s trong slot 6,3 s và mất ý "điều tôi thích nhất".
- Tôi thêm quy tắc "Cut only what the budget needs: land close to it, not far under", rồi chạy **lần 2** với giới hạn 1 call: 1 request, 3,2 s, $0,000212, không cần repair, `unfit_ids` rỗng.

| slot | slot ms | trước (ms ước lượng) | sau (ms ước lượng) |
|---|---|---|---|
| 6 | 7129 | 8200: Làm việc thêm giờ lâu dài sẽ khiến nang tóc mất sức sống, dần trở nên cứng. Chúng ta thường nghĩ là do áp lực lớn, thực ra đều là do thức khuya âm thầm phá hủy sức khỏe nang tóc. | 6000: Làm thêm giờ lâu dài khiến nang tóc mất sức sống, dần cứng lại. Ta thường nghĩ do áp lực, thực ra thức khuya âm thầm phá hủy nang tóc. |
| 9 | 6345 | 7800: Thương hiệu Mandi này độ chuyên nghiệp không cần tôi nói nhiều. Chai dầu gội chống rụng tóc của nó, điều tôi thích nhất là chai đóng chân không, giữ thành phần chính không bị oxy hóa, | 6800: Thương hiệu Mandi không cần nói nhiều về độ chuyên nghiệp. Dầu gội chống rụng tóc này tôi thích nhất ở chai chân không, giúp thành phần chính không bị oxy hóa, |
| 11 | 6608 | 7600: là loại mùi thơm nhạt nhạt. Hương thơm nhẹ nhàng, sau khi gội đầu cảm thấy rất sảng khoái. Nó còn được nghiên cứu chuyên biệt để đối phó với tình trạng lỗ chân lông bị xơ cứng, | 5200: Mùi hương nhè nhẹ, gội xong rất sảng khoái. Sản phẩm còn được nghiên cứu riêng để xử lý tình trạng lỗ chân lông xơ cứng, |
| 13 | 5475 | 6400: chống rụng tóc từ gốc. Khi sử dụng một thời gian, rõ ràng cảm thấy tóc rụng ít hơn, chân tóc cũng vững chắc hơn, giống như tập luyện vai và lưng, | 4200: chống rụng tóc từ gốc. Dùng một thời gian, tóc rụng ít hẳn, chân tóc vững hơn, như tập vai lưng, |

Đánh giá:

- Chữ "Mandi" được giữ, các câu không trôi sang slot khác, và câu nối tiếp ở slot 13 vẫn khớp với câu trước.
- Slot 11 mở đầu bằng chữ hoa "Mùi", trong khi câu gốc là phần nối tiếp của slot 10. Lỗi nhỏ.
- Script và kết quả nằm ở:
  - `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/shortener-check/real_check.py`
  - `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/shortener-check/result-run1.json`
  - `/private/tmp/claude-502/-Users-vietphung-Data-reup-flow/07f62d39-b5c7-4084-a633-0d769a53fb9d/scratchpad/shortener-check/result.json`

## Lưu ý

- **Ở 6 âm tiết/s, 7618 không có slot nào tràn**, nên bước này chỉ thật sự chạy khi giọng đọc chậm hơn hoặc khi review sau TTS phát hiện slot lỗi. Bước 7 của workflow là "rút gọn lại đúng các slot lỗi": nếu review dựa trên thời lượng TTS thật mà ước lượng của câu vẫn vừa, `shorten` sẽ không gửi slot đó. Phase 4 có hai cách: truyền một rate thấp hơn, đo từ clip thật, hoặc cần thêm tham số "ép các id này". Tôi chưa thêm tham số đó vì nó nằm ngoài phạm vi phase này.
- Các helper `_digits_kept` (`review_checks`) và `_is_foreign` (`vi_syllables`) đang được import dù là hàm private, vì phase này không được sửa hai file đó. Nên công khai chúng khi có dịp.
- Lỗi chữ hoa ở đầu câu nối tiếp (slot 11) chưa được code kiểm.
- `ruff check .` báo lỗi ở `activities/plan_slots.py`, file của phase 2.
- Ngoài phạm vi được giao, tôi thêm hai file test: `tests/support/slot_shortener_flow.py` (workflow phụ cho test) và `tests/workflows/test_slot_shortener_flow.py`.

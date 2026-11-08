# Eval Localizer, Dubber và Reviewer

Dubber và Reviewer theo cùng chuỗi của Localizer: lấy input thật → chốt các chiều
→ tạo/validate ca tổng hợp và đối chứng → upload dataset → baseline Langfuse
→ nhãn người để hiệu chỉnh → so sánh trên dataset giữ nguyên.

Localizer giữ các lệnh `build_dataset.py`, `synth_scenarios.py`, `run_experiment.py`.
Các lệnh dưới đây chạy từ `services/localization`, dùng `.venv/bin/python` hoặc
`uv run python`. Đọc khóa từ `.env`, không cần chép khóa vào lệnh.

## Bộ ca và phần được đo

Người dùng chốt các chiều ngày 2026-11-01. Ca viết tay có nguồn Trung và câu Việt,
không lấy câu trả lời của model làm đáp án. Kiểm tra tên, số, slot do code chấm;
ý nghĩa và độ tự nhiên của Dubber dùng rubric/judge giống Localizer.

| Dataset | Số ca | Nội dung |
| --- | ---: | --- |
| `dubber-synth-v1` | 15 | 6 coverage, 9 challenge: độ chật slot, tên, số, CTA, nhiều dòng có id rời, reviewer feedback, injection trong feedback |
| `dubber-length-v1` | 2 | Câu dài nhiều mệnh đề, tên/số/CTA, hai slot khác nhau; bổ sung riêng để giữ nguyên core v1 |
| `reviewer-synth-v1` | 31 | 22 coverage, 9 challenge: đúng/sai nghĩa, thiếu tên/số, chữ không hiển thị, timing, cutoff, âm lượng/peak, caption, subtitle sót/đã che, biên ngưỡng, 13 dòng qua hai batch, injection được dịch như dữ liệu |
| `dubber-v1` | 4 | Context thật từ history, thời lượng đầu vào đã đo và voice gốc; chỉ có 4 history đủ dữ liệu |
| `reviewer-v1` | 5 | Script/dub/edit spec/subtitle thật, âm lượng đo từ artifact; **chưa có nhãn người** |

Runner dùng production `build_dubber` và giới hạn tools/code; single dùng cùng prompt,
assessment, merge, tối đa một repair và giới hạn production. Reviewer gọi chính
`ReviewRenderActivity._judge`, gồm các check production và Jev với batch 12 dòng.
Không sửa agent production hay thứ tự activity của workflow.

`--speech simulated` dùng `estimate_ms` cho lời viết lại, kể cả input lấy từ video thật.
`--speech real` gọi TTS, chia/ghép pieces giống production rồi đo WAV. Không trộn hai
loại run để kết luận độ khớp giọng thật. Eval Reviewer cung cấp phép đo, không phải
kiểm tra OCR trên ảnh hay video render mới; chế độ vision `llm` chưa được hỗ trợ.

## Tạo và chạy

```bash
python eval/agent_scenarios.py generate --agent dubber --file eval/scenarios/dubber-synth-v1.jsonl
python eval/agent_scenarios.py validate --agent dubber --file eval/scenarios/dubber-synth-v1.jsonl
python eval/agent_scenarios.py upload --agent dubber --file eval/scenarios/dubber-synth-v1.jsonl
python eval/agent_experiment.py --agent dubber --dataset dubber-synth-v1 --mode code \
  --out eval/results/dubber.json

python eval/agent_scenarios.py generate --agent reviewer --file eval/scenarios/reviewer-synth-v1.jsonl
python eval/agent_scenarios.py validate --agent reviewer --file eval/scenarios/reviewer-synth-v1.jsonl
python eval/agent_scenarios.py upload --agent reviewer --file eval/scenarios/reviewer-synth-v1.jsonl
python eval/agent_experiment.py --agent reviewer --dataset reviewer-synth-v1 \
  --out eval/results/reviewer.json

python eval/agent_scenarios.py generate --agent dubber --suite length --file eval/scenarios/dubber-length-v1.jsonl
python eval/agent_scenarios.py upload --agent dubber --suite length --file eval/scenarios/dubber-length-v1.jsonl
python eval/agent_experiment.py --agent dubber --dataset dubber-length-v1 --mode code \
  --out eval/results/dubber-length.json
```

`--model` đổi model; `--mode single|tools|code` đổi Dubber. Với single có thể dùng
`--reasoning off`; tools/code giữ cách reasoning production, dùng `default`.
Một run ghi model, prompt/source SHA, dataset SHA, mode, reasoning, judge, loại speech
và cấu hình TTS khi đo thật. Giữ `--run-name` để tiếp tục đúng experiment; tên trùng
mà input/config/source khác sẽ bị từ chối. Item agent/judge đã lỗi được giữ là lỗi;
muốn thử lại chúng thì dùng tên run mới. Judge lỗi không làm mất output Dubber.

Lấy input thật, rồi chạy trên tên dataset tương ứng:

```bash
python eval/build_agent_dataset.py --agent dubber --videos 4 --out eval/results/dubber-v1.jsonl
python eval/build_agent_dataset.py --agent reviewer --videos 5 --out eval/results/reviewer-v1.jsonl
python eval/agent_experiment.py --agent dubber --dataset dubber-v1 --out eval/results/dubber-real.json
python eval/agent_experiment.py --agent reviewer --dataset reviewer-v1 --out eval/results/reviewer-real.json
```

Builder cần Postgres, Temporal UI; Reviewer còn cần S3 và ffmpeg. S3 API của compose
chỉ mở trong network: chạy builder trong image worker, mount thư mục `eval` vào
`/app/services/localization/eval`, font vào `/assets/fonts`, dùng env của compose,
`S3_ENDPOINT=http://minio:9000`, `S3_FORCE_PATH_STYLE=true`,
`--db-host postgres:5432 --temporal-ui http://temporal-ui:8080 --langfuse http://langfuse-web:3000`.
Không khởi động worker/starter để lấy dữ liệu. Build lại dataset thật có thể cập nhật
input theo generation mới: tạo dataset v2 nếu còn muốn so với baseline v1.

## Baseline 2026-11-01

Dubber và judge: `openai/gpt-6-luna`; Reviewer: `typesafe/jev-1.13`, model trả về
`typesafe/jev-1.13-20260917`, threshold 0.8. Tất cả run dưới đây đã kiểm tra qua API
Langfuse tại `http://127.0.0.1:3100`. Số fidelity/naturalness đã đổi từ 1–5 sang 0–1.

| Run name | Kết quả | Chi phí USD, gồm judge |
| --- | --- | ---: |
| `dubber-code-1-baseline-261101` | 15/15 hoàn tất. Coverage: fit 1.00, fidelity 1.00, naturalness 0.958. Challenge: fit 0.889, fidelity 0.861, naturalness 0.750. Tên/số bắt buộc và không thêm contact đều 1.00 sau rescore | 0.00989937 |
| `dubber-code-1-long-261101` | 2/2 hoàn tất, fit/tên/số/contact 1.00; fidelity và naturalness cùng 0.625 | 0.00176476 |
| `dubber-code-1-real-inputs-261101` | 4/4 hoàn tất, fit mô phỏng 1.00; fidelity 0.8125, naturalness 0.625 | 0.002546725 |
| `reviewer-jev-1-baseline-261101` | 31/31 hoàn tất; verdict và route chính xác 31/31; 20 TP, 0 FP, 0 FN | 0.0010395 |
| `reviewer-jev-1-real-261101` | 5/5 hoàn tất; chưa chấm accuracy vì chưa có gold | 0.000140616 |

Tổng chi phí API ghi nhận: **$0.015390971**. Raw report, trace id và phiếu người ở
`eval/results/` (local; không đưa raw transcript vào git).

Nhận xét ban đầu: một ca slot chật giữ tên nhưng vẫn tràn; một ca khác bị judge trừ
vì mất chủ ngữ. Câu dài giữ được tên/số nhưng judge thấy mất chi tiết. Chưa đổi prompt
production dựa trên mẫu nhỏ này. Điểm Reviewer 1.00 chỉ nói về bộ lỗi gieo sẵn khá dễ;
chưa chứng minh chất lượng tổng quát trên video thật.

Một lỗi bộ chấm đã sửa: `30` viết thành `ba chục` phải được tính là giữ số; dùng
`missing_numbers` production thay so chuỗi. Đã rescore 15 output đã lưu, không gọi model:

```bash
python eval/rescore_agent_eval.py --dataset dubber-synth-v1 \
  --report eval/results/dubber-code-1-baseline-261101.json
```

Input phải khớp trace của dataset run. Report giữ nguyên provenance lúc chạy và ghi
SHA của bộ chấm riêng. Không dùng sửa bộ chấm để âm thầm thay input.

## Nhãn người và so sánh tiếp theo

Đã xuất 12 item mỗi agent, cân bằng pool bằng seed 42, che điểm judge, chưa gửi nhãn.
Phiếu đọc: `eval/results/human-review-261101.md`. Điền các trường `labels` trong
`dubber-human-261101.json` hoặc `reviewer-human-261101.json`, giữ `null` khi chưa chấm.

```bash
python eval/human_agent_eval.py export --report eval/results/dubber-code-1-baseline-261101.json \
  --out eval/results/new-dubber-human.json
python eval/human_agent_eval.py import --report eval/results/dubber-code-1-baseline-261101.json \
  --out eval/results/dubber-human-261101.json
```

Dubber: fidelity/naturalness 1–5, injection_ok 0/1. Reviewer: verdict_correct và
routing_correct 0/1. Import kiểm tra toàn bộ nhãn trước khi ghi; `null` không được
chuyển thành pass. Bước hiệu chỉnh judge với người vẫn **chưa hoàn tất**.

Giữ dataset và judge; mỗi lần đổi một trong model/prompt/reasoning/mode. Tách
coverage/challenge/real và simulated/real speech khi đọc điểm. Dataset v1 giữ nguyên;
thêm ca mới vào dataset mới như bộ `dubber-length-v1`. VoiceStudio không kết nối được
trong phiên baseline, nên bước TTS thật và nghe kiểm tra còn mở. Chưa có thử nghiệm
so sánh để kết luận code tốt hơn single/tools, hoặc để đổi threshold của Jev.

Kiểm chứng: 168 test eval/agent liên quan pass; ruff sạch trên các file mới.

---
phase: 9
title: "Score và eval trên Langfuse"
status: pending
priority: P2
effort: "1d"
dependencies: [8]
---

# Phase 09: Score và eval trên Langfuse

<!-- Updated: Red Team 2026-10-26 - thêm naturalness, bỏ glossary_consistency, kiểm AC7 qua API, span provider không chứa nội dung -->

## Goal

Hai mục tiêu:
- Mỗi run có score gắn trên trace của chính nó (AC7).
- Có quy trình eval chạy lặp lại được để so phiên bản của Localizer (AC8).

## Files to Create / Modify

- `src/localization/observability/scores.py`
- `src/localization/observability/provider_spans.py`
- `src/localization/activities/publish.py`
- `services/localization/eval/build_dataset.py`
- `services/localization/eval/run_experiment.py`
- `services/localization/eval/evaluators.py`
- `tests/observability/test_trace_completeness.py`

## Tasks & Steps

1. **Liên kết trace.** Activity `publish` lấy trace id từ OTel context, rồi ghi score qua Langfuse public API.
2. **Score.**
   - Từ Reviewer: `fidelity`, `timing`, `visual`.
   - Deterministic: `overflow_rate`, `mean_stretch`, `residual_han_cues`, `review_rounds`.
   - Boolean: `needs_human`, `safety_flag`.
3. **Span provider.**
   - Tên span là `provider.{stt|tts|ocr}`.
   - Thuộc tính: `provider`, `model`, `input_units`.
   - Không chứa text, audio, key hay URL. Canary của phase 03 phủ cả span này.
4. **Dataset.** `build_dataset.py --videos N`: lấy transcript và subtitle của các run đã publish,
   tạo dataset `localizer-v1`.
5. **Experiment.** `run_experiment.py --model X --prompt-version Y` chạy Localizer như agent thường, ngoài Temporal.

   | Evaluator | Cách chấm |
   |---|---|
   | `fit_ratio` | Deterministic, dùng `vi_syllables` |
   | `fidelity` | LLM-as-judge, `JUDGE_MODEL`, có rubric |
   | `naturalness` | LLM-as-judge, `JUDGE_MODEL`, có rubric |

## Verification

**Test tự động.** `uv run pytest tests/observability` với exporter in-memory, chỉ phủ các span Python.
- Phải đủ các span `ingest`, `provider.stt`, `provider.ocr`, model request của Localizer, `provider.tts`, `review_render`.
- Bỏ interceptor ở worker thì test đỏ.

**Kiểm AC7 đầy đủ, gồm span `render` từ worker Node:** làm ở phase 10, qua API của Langfuse.

**Thủ công.**
1. Build dataset 5 video.
2. Chạy experiment hai lần với hai model khác nhau.
3. Trên Langfuse thấy hai dataset run có đủ ba score (AC8).
4. Chụp màn hình, lưu vào `reports/`.

## Risk

5 video chỉ đủ chứng minh quy trình chạy được, chưa phải benchmark.

## Rollback

Chỉ thêm span và score, không đổi hành vi của workflow.

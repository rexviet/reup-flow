---
title: Contextual Localizer và intentional omission
date: 2026-11-09
summary: Hoàn tất Phase 02 theo ngoại lệ phụ thuộc; giữ legacy default và review rollout chưa bật.
---

# Contextual Localizer và intentional omission

## Kết quả

Triển khai policy contextual được ghi trong history, metadata translated/inferred/unresolved,
repair có transcript và neighbors, giữ metadata qua JSON/Dubber. Unresolved không gọi TTS,
không tạo caption, dùng BGM hoặc silence đủ thời lượng và không dùng original vocals.

Kiểm chứng: 318 checks cho Localizer/audio/contracts/architecture; 422 workflow/replay cũ/Vbee;
164 layout/reviewer trong image raqm. Sửa cuối merge JSON được kiểm lại bằng 49 unit tests
và 2 contextual workflow/replay tests. Ruff và diff checks đạt.

## Quyết định và bài học

User cho phép triển khai Phase 02 dù Phase 01 chưa accepted, giữ review delivery disabled.
Ngoại lệ được ghi trong plan; không tuyên bố Studio gate pass. Không commit/publish hoặc đổi DB.
Default fields được materialize qua durable JSON, nên không dùng model_fields_set làm bằng
chứng rằng repair có hay không có metadata; giữ provenance dựa trên trạng thái của record.

Test harness phải giữ repo depth cho các migration paths và import source mới bằng PYTHONPATH.
Pillow raqm được kiểm trong image phù hợp, không giảm assertions. Các test kỳ vọng exact field
và untrusted block được cập nhật theo hợp đồng; không bỏ kiểm tra. Simplifier thực hiện scoped edits;
review acceptance/contracts thực hiện inline. Không còn container hoặc server tạm của lượt này.

## Phần còn lại

Phase 03–05 chưa thuộc lượt thực thi này. Studio Host/Origin và browser close/reopen vẫn mở;
real-model semantic evaluation chờ source transcript. Báo cáo:
[Phase 02](../reports/cook-261109-2040-contextual-localizer-phase-02.md).

AgentWiki publish skipped.

> Historical work record — not durable authority. Prefer docs/specs/ADRs for current decisions.

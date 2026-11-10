---
phase: 3
title: "Review bundle bền vững và workflow không render"
status: pending
priority: P1
effort: 4d
dependencies: [2]
---

# Phase 3: Review bundle bền vững và workflow không render

## Goal

Cô lập artifact từ đầu mỗi generation; giao project bền vững với revision bất biến và durable HEAD, không gọi render video cuối.

<!-- Updated: Red Team Review 2026-11-09 - generation isolation, delivery selector, immutable snapshots and durable HEAD -->

## Contracts và quyết định

- `EditSpec`, legacy spec hash và `PublishResult.review_key` MP4 giữ nguyên. Thêm model `ReviewProjectManifest`, `ReviewHead`, `ProjectRef` và optional `project_key`; exporter check cả schema mới lẫn schema EditSpec cũ.
- `project_key` lưu **HEAD key ổn định** `review/{aweme}/g{generation}/head.json`, không phải manifest của revision ban đầu. HEAD chứa revision ID, manifest key/checksum và source identity. Manifest ở `review/{aweme}/g{generation}/r{revision_id}/manifest.json` là create-only. Bridge mở từ durable HEAD, không coi thư mục local là nguồn sự thật.
- Manifest giữ voice/provider/model/engine references của bản dub gốc để revoice dùng cùng cấu hình/cache identity; không lưu API keys hoặc Vbee configured secret.
- Revision ID do server tạo, không do client chọn. Media bất biến dùng content-addressed keys dưới `review/{aweme}/g{generation}/assets/`; manifest chỉ tham chiếu revision hiện tại hoặc assets của đúng generation đó, kèm checksum. Không tham chiếu `source/`, `work/`, `cache/` hay generation khác. Một bản export/materialize thu đủ dependency thành folder tự chứa.
- Upload dependency trước, conditional-create manifest sau cùng, rồi create HEAD lần đầu bằng `If-None-Match: *`. Manual edit/revoice chỉ đổi HEAD bằng `If-Match` ETag. Chỉ sau HEAD hợp lệ mới ghi `runs.project_key`. Initial publish retry không reset HEAD đã có hoặc ghi đè revision người review đã sửa.
- `ready_for_review` là terminal bình thường, `needs_human` là quality exception. Cả hai giữ project, không có `output_key`. Blank dịch không biến thành `published_original`; các early gate cũ và Vbee giữ semantics hiện có.

## Files sở hữu

Các file bên dưới dùng đường dẫn tuyệt đối; phần Steps/Verification dùng cwd đã ghi.

- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/models.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/__init__.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/export.py`; create `/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/review_project.py` và `/Users/macbook/Data/reup-flow/contracts/review-project.schema.json` tại repository root — additive contracts/schema/barrel exports.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/storage/object_store.py` — generation-scoped work helper, conditional writes, conditional HEAD và source identity reads; không thay nghĩa helper legacy mặc định.
- Modify toàn bộ work producer/consumer tại `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/`: `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/ingest.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/transcribe.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/detect_subtitles.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/assemble_script.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/script_document.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/dub_script.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/assemble_audio.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/layout_captions.py`; `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/render.py` vẫn dùng keys legacy cho nhánh legacy. Không chỉ sửa destination bundle.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/starter/outbox_starter.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/config.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/contracts/models.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/workflows/video_localization.py` — freeze delivery mode, source snapshot identity và route deterministic.
- Create `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/review_project.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/srt_document.py`; modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/publish.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/workers/run.py` — build/register/publish project.
- Create `/Users/macbook/Data/reup-flow/services/localization/migrations/261108-review-project-key.sql`, `/Users/macbook/Data/reup-flow/services/localization/migrations/261108-review-project-key.down.sql`; chỉ áp production ở Phase 5.
- Create `/Users/macbook/Data/reup-flow/services/localization/tests/contracts/test_review_project.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/activities/test_review_project.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/workflows/test_review_project_flow.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/workflows/test_generation_isolation.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/storage/test_review_snapshots.py`; modify existing `/Users/macbook/Data/reup-flow/services/localization/tests/activities/test_publish.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/starter/test_outbox_starter.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/replay/test_replay.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/workers/test_agent_wiring.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/test_migration.py`.

## Delivery mode và source provenance

1. Phase 2 thêm optional input `delivery_mode=legacy_render|review_project`, default **legacy_render** cho input lịch sử. Starter inject từ `LOCALIZATION_DELIVERY_MODE`; config rollout ban đầu legacy. Chỉ flow VoiceStudio tiêu thụ mode mới; Vbee không tự chuyển sang project.
2. Mode đóng băng trong workflow input trước khi start, không đọc env khi replay. Nhánh mới yêu cầu input mode review_project và patch marker mới. Rollback config chỉ ảnh hưởng dispatch mới, giữ decoder/worker mới cho histories đã ghi mode/marker mới. Không dùng `workflow.patched()` làm feature flag.
3. Nhánh mới capture source identity (ETag/version/checksum khả dụng) và source snapshot có điều kiện trước khi prep chạy song song; OCR/ingest đều đọc snapshot đó. Giữ `video_key` SourceKey cũ đúng type; dùng reference mới nếu snapshot key thuộc work, không ép work key vào SourceKey. Source thay đổi giữa capture/copy phải lỗi rõ ràng, không copy nhầm bản.
4. Tất cả work key của nhánh mới có generation ngay tại producer, gồm transcript, subtitle doc, scripts, dub-segments, BGM và mixed audio; source copy lấy từ captured snapshot. Script reference/version và provenance được kiểm tra tại consumer. Legacy input/artifact giữ key/decoder/hash cũ, không tự migrate dữ liệu đã có.

## Build và commit protocol

1. Build từ final script/dub cùng version; `translated|inferred|unresolved` và source segment IDs giữ nguyên. Caption đã lưu mới nhất là nguồn text/timing cho preview và SRT; active audio lưu spoken text/placement riêng để thể hiện mismatch sau manual edit.
2. Project video luôn mute. Playback dùng **một mixed audio active** tạo từ BGM và active cue clips, không bật đồng thời mixed track với từng cue clip hoặc source vocals. Clip assets vẫn được giữ để revoice/rebuild mix mà không TTS lại câu khác. All-blank dùng BGM hoặc silence đúng duration.
3. Project HTML giữ stable cue IDs, font/style có thể chỉnh, yellow text/black outline. Source mask blur + dải nền full-width theo source region/cues, kể cả khi caption Việt rỗng; không sửa legacy cover rules.
4. Dùng primitive create-only thực sự cho immutable asset/manifest; existing key chỉ tái sử dụng nếu checksum khớp. Kiểm chứng MinIO/S3 hỗ trợ conditional put/multipart completion cho large file; không giả định `copy_object` có destination precondition và không fallback overwrite vô điều kiện. HEAD cập nhật CAS ETag, conflict không thay bất kỳ byte published nào.
5. Auto review đọc dữ liệu, không fallback render video. Inferred/unresolved/overflow/refusal, quality fail hoặc reviewer unavailable -> needs_human; clean pass -> ready_for_review. Cả hai chờ người review, không auto export/publish.
6. Serialize cue/reason như dữ liệu JSON, không executable interpolation. HTML/JS encoder escape đóng script; companion dùng textContent/value. CSP và payload regression ở Phase 4.
7. Crash trước HEAD chỉ để orphan candidate chưa được discovery; retry kiểm tra ID/checksum và publish đúng một HEAD. Crash sau HEAD/trước DB upsert: upsert lại stable HEAD pointer. Reopen luôn resolve HEAD; không tự chọn revision mới nhất theo tên/listing.

## Verification

```bash
cd /Users/macbook/Data/reup-flow/services/localization
uv run python -m localization.contracts.export --check
uv run pytest -q tests/contracts/test_review_project.py tests/activities/test_review_project.py tests/activities/test_publish.py
uv run pytest -q tests/workflows/test_review_project_flow.py tests/workflows/test_generation_isolation.py tests/storage/test_review_snapshots.py
uv run pytest -q tests/starter/test_outbox_starter.py tests/replay/test_replay.py tests/workers/test_agent_wiring.py tests/test_migration.py
uv run ruff check src tests
```

Thử g1/g2 cùng aweme interleave ngay từ prep/write, assert mọi artifact/provenance đúng generation. Crash injection ở upload/manifest/HEAD/DB, hai writer cùng revision, HEAD ETag stale, cold-open không còn local folder, backend 409/412. Thử selector legacy/review và rollback khi run project đang mở. All-blank có zero segment synthesis; calibration voice_rate có thể đã gọi TTS. BGM có thể còn leakage từ tách stem; không hứa tuyệt đối xóa mọi âm giọng gốc.

## Acceptance

- [x] Source/work assets cô lập generation từ producer; interleave hai generation không trộn dữ liệu; legacy replay/hash/Vbee pass.
- [x] Conditional-create/CAS chứng minh trên backend thật; immutable manifest không bị overwrite; cold-open từ durable HEAD khôi phục đúng revision.
- [x] Clean/flagged run có HEAD project_key, không render/output; schemas được export/check, early gate cũ không đổi.

## Risk / rollback

Backend không hỗ trợ conditional operation là stop signal của storage gate: chọn cơ chế transactional tương đương và chứng minh trước enable, không hạ invariant xuống check-then-write. Thiếu source/provenance hoặc checksum mismatch làm build fail rõ ràng. Rollback `LOCALIZATION_DELIVERY_MODE=legacy_render` ở starter, giữ worker có cả hai branch và bundle/HEAD. Không rollback binary về bản không hiểu histories mới; DB down chỉ sau backup và khi không còn consumer mới.


## Execution evidence — 2026-11-09

Acceptance checks completed under the requested `ak-cook --auto` invocation.
[Implementation, real MinIO conditional-write proof, interleaved generation checks,
legacy/Vbee replay and scratch migration evidence](../reports/cook-261109-2113-hyperframes-phase-03.md).
Review delivery remains `legacy_render` by default; production migration/activation
belongs to Phase 05. Phase 01 Studio gates remain open.

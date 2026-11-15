# Review failure modes: HyperFrames review và contextual Localizer

Ngày review: 2026-11-09 (Asia/Ho_Chi_Minh)
Phạm vi: chỉ review plan; đã đọc đủ `plan.md`, 5 file `phase-*.md`, brainstorm accepted, hai research report và các source/replay baseline được giao. Không chạy test/lint/build và không sửa plan/code.

## Kết luận

Kế hoạch chưa sẵn sàng để thực thi an toàn. Có 1 lỗi Critical có thể làm migration mới bị bỏ qua nhưng ledger vẫn báo đã áp dụng, và 2 lỗi High khiến rollback/restart không có đường phục hồi xác định. Các phần single-context, explicit unresolved, giới hạn `atempo <=1.15x`, giữ active audio cũ và không auto render/publish đã được phản ánh đúng; không ghi chúng thành finding chỉ vì code hiện tại chưa có tính năng.

## Findings

### 1. Critical — Bootstrap ledger có thể đánh dấu migration `project_key` là đã chạy dù schema chưa đổi

**Vị trí plan:** `phase-05-validation-and-rollout.md:18`, `:32-33`, `:67`; `phase-03-review-project-and-workflow.md:29`, `:46`.

**Bằng chứng nguồn:** `scripts/apply-migrations.sh:30-38` tạo ledger khi thiếu; nếu chỉ thấy bảng `public.videos` tồn tại, script chèn **mọi filename trong mảng hiện tại** vào ledger. Plan yêu cầu append migration mới vào chính mảng đó. Sau đó loop tại `scripts/apply-migrations.sh:42-48` sẽ skip filename đã được seed. Ngoài ra DDL và ledger insert là hai lệnh riêng tại `scripts/apply-migrations.sh:50-56`, nên crash sau DDL nhưng trước ledger để lại trạng thái nửa commit.

**Kịch bản hỏng:** restore một DB cũ có tables nhưng chưa có `applied_migrations`, rồi deploy code có `261108-review-project-key.sql`. Script seed cả filename mới mà không chạy file; downloader/localization sau đó truy cập `project_key` và fail vì column/status chưa tồn tại, trong khi lần chạy thứ hai vẫn báo skip. Clone rehearsal trên DB đã có ledger không bắt được đường này.

**Sửa plan:** Phase 5 phải sở hữu sửa runner, không chỉ append filename. Seed chỉ một baseline cố định đã chứng minh bằng schema/version, hoặc chạy migration mới idempotently sau schema introspection. Migration + ledger record phải atomic trong cùng transaction; file tự mở transaction phải tự ghi ledger trong transaction đó hoặc bị runner từ chối. Thêm test bắt buộc cho (a) DB có tables nhưng không ledger và thiếu column mới, (b) crash ngay sau DDL trước ledger, (c) rerun sau cả hai trường hợp.

### 2. High — “Disable new dispatch” chưa có switch; patch marker không phải feature flag rollback

**Vị trí plan:** `plan.md:31`, `:59`; `phase-03-review-project-and-workflow.md:44`, `:79`; `phase-05-validation-and-rollout.md:76`, `:79`.

**Bằng chứng nguồn:** nhánh hiện tại được quyết định trực tiếp bởi `workflow.patched(...)` trong `video_localization.py:351-370`; với execution mới marker được ghi và nhánh mới là true, không có operator toggle. `LocalizationInput` hiện chỉ chứa identity/video/lang/voice (`contracts/models.py:20-26`). Starter luôn tạo input đó (`starter/outbox_starter.py:239-251`) và start `VideoLocalizationWorkflow` cố định (`:330-344`). Config chỉ chọn `voicestudio|vbee`, không chọn legacy project-vs-render delivery (`config.py:32-35`). Replay baseline còn chứng minh command-order change không marker gây nondeterminism (`tests/replay/test_replay.py:159-191`).

**Kịch bản hỏng:** canary phát hiện nhánh project sai. Run mới tiếp tục ghi marker vì không có delivery selector. Roll back binary về code cũ có thể làm histories đã ghi marker không còn implementation tương thích; giữ worker mới lại tiếp tục nhận cả run mới vì cùng workflow type/task queue. Câu “disable dispatch” không ánh xạ tới file/config/owner/command nào trong plan.

**Sửa plan:** định nghĩa switch cụ thể tại điểm dispatch, ví dụ additive `delivery_mode=legacy_render|review_project` được đóng băng trong `LocalizationInput`, hoặc workflow type/task queue riêng. Nêu owner, default, config source và lệnh rollback. Worker phải route deterministically theo input đã ghi: histories `review_project` tiếp tục trên worker tương thích, còn dispatch mới chuyển về legacy mà không sửa nghĩa marker. Thêm replay/canary test cho toggle trước start, rollback khi có run đang chạy, và restart worker sau khi marker đã ghi.

### 3. High — Snapshot/revoice chưa có durable HEAD và retry contract, nên revision thành công có thể mất khỏi đường discovery sau restart

**Vị trí plan:** `phase-03-review-project-and-workflow.md:34-36`, `:45-52`, `:75`; `phase-04-studio-editing-and-line-voice.md:34-36`, `:44`, `:61-67`, `:84`; `phase-05-validation-and-rollout.md:28`, `:69`.

**Bằng chứng nguồn/flow:** Phase 3 publish DB bằng một `project_key` sau manifest-last; DB publish hiện là một exact-pointer upsert autocommit (`activities/publish.py:44-60`, `:81-97`, `:156-171`). Phase 4 lại mô tả save là upload immutable snapshot rồi chỉ advance **local** current pointer (`phase-04...:34`), trong khi Phase 5 discovery chỉ đọc DB project pointer (`phase-05...:28`). Không bước nào CAS một durable current revision sau manual edit/revoice. MinIO giữ `review/` không expiry (`infra/minio/init.sh:78-82`), nhưng durability của bytes không làm revision mới discoverable. Revoice ID được derive từ project/cue/hash/base revision (`phase-04...:36`), song plan không nói retry sau lost HTTP response sẽ attach/read kết quả workflow đã tồn tại hay xử lý `AlreadyStarted` thế nào.

**Kịch bản hỏng:** save/revoice upload xong `r2`, process chết trước local pointer. Sau restart/clean materialization, `/api/reviews` vẫn đưa `project_key` của `r1`; `r2` tồn tại nhưng orphan khỏi đường mở project. Nếu client retry cùng request, workflow ID cũ có thể đã running/completed; nếu tạo request khác có thể gọi provider lại, còn nếu chỉ trả conflict thì draft không bao giờ commit. Nếu revision number lấy từ local pointer cũ, retry còn có nguy cơ đụng prefix được gọi là immutable.

**Sửa plan:** chốt một protocol duy nhất: upload dependencies vào candidate prefix -> upload manifest cuối -> CAS durable HEAD (DB row với `expected_revision`, hoặc head object với ETag/version) -> cập nhật local pointer chỉ như cache. Mở/restart phải materialize từ durable HEAD; orphan trước HEAD phải recover/GC được. Revoice cần idempotency key và semantics rõ: retry attach/await/read completed workflow, rồi commit bằng CAS base revision; stale result giữ active audio/draft như quyết định sản phẩm. Test crash injection tại dependency, manifest, durable HEAD, local cache; lost response sau Temporal start và sau workflow completion; restart không còn local folder; hai save/revoice đồng thời chỉ một HEAD thắng.

## Fact/flow checks

Đã kiểm tra **18** claim độc lập với source/baseline:

1. Marker hiện tại và vị trí branch trong workflow.
2. Legacy review path hiện vẫn layout/render/review/publish.
3. Publish hiện yêu cầu render cho outcome không thuộc nhóm early terminal.
4. DB run upsert hiện autocommit và lưu exact output/review pointer.
5. `LocalizationInput` chưa có delivery selector.
6. Starter đóng băng lang/voice lúc start nhưng chưa đóng băng delivery mode.
7. Starter dùng workflow type/task queue cố định và reject duplicate workflow ID.
8. Replay suite replay toàn bộ recorded histories.
9. Replay suite có negative controls cho command-order thiếu marker.
10. Migration list là mảng tĩnh trong runner.
11. Ledger-less bootstrap seed mọi filename hiện có khi thấy tables.
12. DDL execution và ledger insert không atomic với nhau.
13. Localization identity có RW `review/`; downloader chỉ `source/`.
14. `review/` không có lifecycle expiry.
15. Single Localizer hiện fail khi assessment còn unusable.
16. TTS hash hiện gồm provider/model/engine/voice/text/method và có dedupe trong process.
17. Audio assembler hiện đọc clip theo từng `tts_key` và từ chối mixed track silent.
18. Caption layout hiện bỏ text rỗng nhưng kiểm tra script/dub version và text đồng nhất.

## Câu hỏi chưa giải quyết

- Durable HEAD sẽ thuộc DB hay object store, và component nào có quyền CAS nó?
- Rollback selector sẽ là field input, workflow type riêng hay task queue riêng? Plan cần chọn đúng một phương án trước execution.

Status: DONE_WITH_CONCERNS

## Post-amendment sweep — 2026-11-09

Đã đọc lại `plan.md`, cả 5 phase và `review-validation.md` sau khi controller áp dụng 7 findings. Sweep này chỉ kiểm tra consistency của các delta đã duyệt; không review lại source, không chạy test và không mở rộng feature scope.

Kết quả: **closed — không còn contradiction hoặc blocker cụ thể trong 7 delta**.

- Migration đã yêu cầu schema evidence cho DB legacy không ledger, DDL + ledger atomic trong cùng transaction, fail-closed khi không reconcile được và test crash/rerun (`phase-05:30-35`, `:75`).
- Producer/consumer của nhánh mới được cô lập theo generation từ source capture qua work artifacts, kèm provenance và interleave test; legacy/Vbee giữ key/decoder cũ (`phase-03:31-43`, `:66-72`).
- `delivery_mode` được ghi vào input với legacy default, starter sở hữu toggle/canary/rollback; worker giữ cả hai branch và status consumers API/web/smoke được liệt kê (`phase-02:37`, `:52`, `:86`; `phase-03:40-43`; `phase-05:45-51`, `:81`).
- Asset/manifest là create-only, HEAD là stable locator cập nhật bằng CAS; local pointer chỉ là cache. Cold-open, lost response, `AlreadyStarted`, crash trước/sau HEAD và stale concurrent commit đều có protocol/test (`phase-03:20-23`, `:45-53`, `:66`; `phase-04:41-50`, `:63`).
- Studio được mở bởi launcher trên macOS host; bridge ở container chỉ publish loopback, không mount Docker socket hoặc tự spawn host CLI; PID/port/path lifecycle có owner rõ (`phase-01:46`; `phase-04:18-24`; `phase-05:41-43`).
- Bridge dùng principal chỉ cho `review/` và internal Temporal, không nhận provider/DB/LOC_S3/root secrets; provider resolution nằm ở worker (`phase-04:20-24`, `:46`, `:50`; `phase-05:43`).
- Cue/reason được encode như data, UI dùng `textContent/value`, CSP không `unsafe-inline`; payload HTML/JS/SRT có regression/live gates (`phase-01:48`; `phase-03:52`; `phase-04:49`, `:63`).

Hai clarification của controller cũng nhất quán xuyên phase: caption mới nhất là nguồn preview/SRT trong khi active audio giữ spoken text/timing và mismatch; revoice thành công rebuild một derived mixed track từ clip mới + clip cũ + BGM, không TTS lại cue khác và không phát đồng thời per-cue audio (`phase-03:47-48`; `phase-04:39`, `:47`, `:63`, `:69`).

Các mục Studio pin/adapter, MinIO CAS backend, migration clone, provider live và `raqm` vẫn là technical gates chưa chạy, đã được ghi đúng là điều kiện triển khai chứ không phải quyết định hoặc blocker của plan (`phase-05:83-85`; `review-validation.md:117-122`).

Status sau amendment: DONE

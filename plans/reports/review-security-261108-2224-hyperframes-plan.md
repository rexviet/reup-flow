# Security review: HyperFrames review/contextual Localizer plan

Ngày review: 2026-11-09 (Asia/Ho_Chi_Minh)
Phạm vi: `plan.md`, 5 phase, brainstorm accepted, 2 research report và source liên quan. Đây là review tài liệu; không chạy lint/build/test và không sửa plan/code.

## Kết luận

**DONE_WITH_CONCERNS.** Plan giữ đúng quyết định sản phẩm (companion cùng Mac, loopback-only, không fork Studio, stretch tối đa 1,15×, không auto render/publish), nhưng còn 3 trust-boundary gap cần trở thành yêu cầu/acceptance test trước implementation. Không có finding Critical; có 2 High và 1 Medium.

## Findings

### High — Bridge dùng identity MinIO quá quyền so với HTTP surface mới

- **Plan:** Phase 4, `Boundary / deployment` dòng 20 và `Auth / persistence / data flow` dòng 32; plan chỉ đảm bảo credential không đi vào browser/project, chưa giới hạn credential của chính bridge.
- **Failure cụ thể:** nếu route/path/parser của bridge bị chiếm, identity localization hiện tại có thể đọc source, đọc/ghi/xóa toàn bộ `work/`, `cache/`, `review/`, và ghi `output/`. Kẻ tấn công không chỉ sửa project đang mở mà còn có thể xóa review khác, poison TTS cache/work artifact hoặc ghi output. Bundle Phase 3 đã self-contained trong `review/`, nên bridge không cần quyền này.
- **Bằng chứng source:** compose truyền chung `LOC_S3_ACCESS_KEY_ID/LOC_S3_SECRET_ACCESS_KEY` cho localization (`docker-compose.yml:33-39`); policy localization cấp `GetObject` source, `Get/Put/Delete` cho `work/*`, `cache/*`, `review/*`, và `PutObject` cho `output/*` (`infra/minio/init.sh:46-55`).
- **Fix bắt buộc:** tạo principal/env riêng `BRIDGE_S3_*`; chỉ cho `GetObject` trên `review/*` và `PutObject` vào prefix revision/project được phép, không `DeleteObject`, không `work/cache/output/source`. Container bridge không nhận DB, VoiceStudio, OpenRouter hay root MinIO secrets. Thêm compose/env-policy test chứng minh bridge không đọc/ghi ngoài `review/` và không xóa snapshot.

### High — “Immutable revision” hiện chỉ là quy ước key, không phải write-once guarantee

- **Plan:** Phase 3 `Steps` dòng 41 và `Failure / security model` dòng 52; Phase 4 `Auth / persistence / data flow` dòng 34 và `Steps` dòng 44.
- **Failure cụ thể:** retry/race/bug dùng lại cùng revision key có thể overwrite object/manifest đã được publish. Checksum sau overwrite chỉ chứng minh bytes mới nhất nhất quán, không chứng minh snapshot trước đó chưa bị thay. `expectedRevision` bảo vệ pointer logic nhưng không biến S3 write thành compare-and-create.
- **Bằng chứng source:** `ObjectStore.put_bytes()` gọi `put_object` vô điều kiện (`services/localization/src/localization/storage/object_store.py:109-121`); `put_file()` upload vào key đích không precondition (`:123-126`); `copy()` dùng `copy_object` vào key đích không destination precondition (`:147-157`). Policy hiện còn cho `DeleteObject` trên `review/*` (`infra/minio/init.sh:52`). Validators chỉ kiểm tra grammar/path, không ownership hay immutability (`services/localization/src/localization/contracts/ids.py:49-57`).
- **Fix bắt buộc:** định nghĩa primitive create-only cho manifest/pointer (`If-None-Match: *` hoặc cơ chế CAS tương đương được MinIO/S3 kiểm chứng), media key content-addressed/checksummed, và khi key đã tồn tại chỉ chấp nhận exact checksum thay vì overwrite. Manifest upload last phải là conditional create; pointer advance phải CAS theo base revision. Test phải inject hai writer cùng revision và chứng minh loser không thay bất kỳ byte nào.

### Medium — Plan chưa bắt buộc output encoding/CSP cho cue text đi vào companion và source thực thi

- **Plan:** Phase 1 `Files` dòng 25 và `Data flow to prove` dòng 31; Phase 3 `Manifest / data flow` dòng 34 và `Steps` dòng 42; Phase 4 `Files` dòng 25 và `Steps` dòng 46.
- **Failure cụ thể:** cue text/reason là dữ liệu model/người dùng kiểm soát. Nếu generator nội suy nó vào `captions.js`/HTML hoặc companion render bằng `innerHTML`, payload như `</script><script>…</script>` trở thành stored XSS. Trong companion origin, script có thể dùng session hiện tại để save/revoice; `HttpOnly`, Host/Origin và CSRF không ngăn được script chạy cùng origin.
- **Bằng chứng source:** contract `Text` cho phép chuỗi bất kỳ đến 4000 ký tự (`services/localization/src/localization/contracts/models.py:17`); `LocalizedSegment.text_vi` và `LocalizedLine.text_vi` dùng trực tiếp type đó (`:147-154`, `:186-192`). Chính plan coi source/reviewer notes là untrusted (`phase-02-contextual-localizer-and-empty-lines.md:41`) nhưng không chuyển invariant này sang project generator/UI.
- **Fix bắt buộc:** cue data nằm trong JSON/data file không thực thi; serialize bằng encoder chuẩn và escape `<` khi nhúng trong `<script>`; UI chỉ dùng `textContent`/`value`, cấm `innerHTML`/`eval`; thêm CSP chặt (`script-src` theo file/hash, không `unsafe-inline`). Test payload HTML/JS/SRT độc hại qua save, reopen và export, xác nhận hiển thị literal và không có request phụ/save/revoice.

## Fact-check nguồn

Đã kiểm tra **15 claim** bằng source hiện tại:

1. Node tối thiểu 22: đúng (`package.json:5`).
2. Initial Localizer có toàn transcript: đúng (`agents/localizer_single.py:101-139`, prompt build quanh `:170`).
3. Repair hiện chỉ mang failed lines/glossary, chưa có neighbor/global block: đúng (`agents/localizer_single.py:276-301`).
4. Blank/missing hiện bị phân loại `unusable`: đúng (`agents/localizer_single.py:189-207`, `:245-250`).
5. Durable single-mode fail khi còn unusable: đúng (`agents/temporal.py:353-359`).
6. `LocalizedSegment` hiện chưa có status/reason: đúng (`contracts/models.py:147-169`).
7. `DubSegment.tts_key` hiện bắt buộc: đúng (`contracts/models.py:214-220`).
8. TTS hash gồm method/provider/model/engine/voice/text: đúng (`activities/synthesize.py:51`, `:73-87`).
9. Existing Temporal workflow dùng `workflow.patched()` và có nhánh review-before-render: đúng (`workflows/video_localization.py:1-7`, `:759`).
10. `PublishResult.review_key` hiện là pointer MP4 optional và không nên repurpose: đúng (`contracts/models.py:461-464`, `activities/publish.py:139-171`).
11. Downloader API key là global `APP_GUARD`, chỉ `@Public()` opt-out: đúng (`auth/api-key.guard.ts:17-48`, `app.module.ts:18-23,59`).
12. Architecture test hiện chứng minh Temporal read-only ở downloader: đúng (`test/architecture/http-lane-boundary.spec.ts:208-223`).
13. Downloader MinIO principal chỉ có `source/*`; localization principal rộng hơn như finding 1: đúng (`infra/minio/init.sh:37-55`).
14. `work/` hết hạn 30 ngày, `review/` không có lifecycle: đúng (`infra/minio/init.sh:78-82`).
15. Python `ObjectStore` validate storage-key grammar nhưng write/copy không có create-only precondition: đúng (`contracts/ids.py:49-57`, `storage/object_store.py:109-157`).

Các file Phase 1/3/4/5 được ghi rõ `Create` không bị báo như lỗi “đang thiếu”; findings chỉ nhắm defense mà plan chưa yêu cầu.

## Unresolved questions

1. Token bootstrap do host launcher hay bridge phát hành, TTL/attempt limit/session rotation cụ thể là gì? Không được ghi token vào DB, project, command history hoặc web bundle.
2. Activation switch nào bảo đảm migration/deploy worker mới không tự chuyển run sang nhánh no-render trước canary, và rollback giữ worker tương thích cho history đã ghi marker?

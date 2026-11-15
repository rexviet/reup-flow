---
phase: 4
title: "Companion round-trip và revoice từng cue"
status: pending
priority: P1
effort: 4d
dependencies: [3]
---

# Phase 4: Companion round-trip và revoice từng cue

## Goal

Companion lưu text/timing/SRT và tạo giọng từng cue; Studio host-local chỉnh visual. Save/revoice bền vững qua crash, retry và concurrent edits.

<!-- Updated: Red Team Review 2026-11-09 - host launcher, least privilege, durable CAS and untrusted caption encoding -->

## Topology, quyền và launcher

- Bridge Python ở compose, listen `0.0.0.0` trong container; host chỉ publish `127.0.0.1:3310:3310`. Internal Temporal/MinIO dùng service DNS. Studio trên macOS host dùng loopback port 3002 đã kiểm chứng ở Phase 1; không mở 7233 ra host. Port collision phải xác định owner, không tăng port hoặc kill process không sở hữu.
- Bridge dùng identity S3 riêng, chỉ Get/Put và prefix-scoped List nếu cần trên review/, không Delete, không source/work/cache/output. Không truyền LOC_S3 identity, DB, VoiceStudio, OpenRouter hay MinIO root secret vào bridge. TTS/provider secret chỉ ở worker. Downloader chỉ đọc DB discovery, không đọc bucket review.
- Host script `scripts/review-studio.mjs open <aweme> <generation>` gọi bridge CLI bằng `docker compose exec -T` với argv arrays để materialize project và tạo one-time launch grant. Không mount Docker socket vào container, không để container spawn host process. CLI/server dùng protected runtime grant store ngoài folder project, cùng container UID, atomic consume.
- Host script resolve folder dưới data/review, từ chối symlink/path escape, chạy đúng CLI pin từ package lock của Phase 1, track PID/command/port/project và mở Studio + companion qua macOS open argv. Lệnh stop chỉ dừng PID còn khớp record. Không chạy shell từ text sub hoặc project path. Khi script kết thúc dọn process nó khởi chạy.
- Grant TTL 60 giây, một lần dùng; launcher giữ grant trong memory và URL fragment, không log. Companion xóa fragment trước redeem POST; session HttpOnly/SameSite=Strict, TTL tối đa 8 giờ, rotate khi launch lại. Check exact Host/Origin/CSRF, no CORS wildcard; redact grant/cookie khỏi logs, response no-store và Referrer-Policy no-referrer. Native Studio server cũng phải qua origin/loopback probe ở Phase 1; không coi cookie là cách ly theo port.

## Files sở hữu

Các file bên dưới dùng đường dẫn tuyệt đối; phần Steps/Verification dùng cwd đã ghi.

- Create `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/server.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/auth.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/repository.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/service.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/cli.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/static/index.html`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/static/app.js`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/review_bridge/static/styles.css`.
- Create `/Users/macbook/Data/reup-flow/scripts/review-studio.mjs`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/workflows/revoice_caption.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/activities/revoice_caption.py` — host launcher và durable one-cue regeneration.
- Modify `/Users/macbook/Data/reup-flow/services/localization/src/localization/workers/run.py`, `/Users/macbook/Data/reup-flow/services/localization/src/localization/config.py`, `/Users/macbook/Data/reup-flow/services/localization/pyproject.toml`, `/Users/macbook/Data/reup-flow/services/localization/uv.lock`, `/Users/macbook/Data/reup-flow/services/localization/Dockerfile`, `/Users/macbook/Data/reup-flow/docker-compose.yml`, `/Users/macbook/Data/reup-flow/infra/minio/init.sh`; dependencies HTTP phải pin/lock và thực sự thêm, không giả định image đã có server framework.
- Create `/Users/macbook/Data/reup-flow/services/localization/tests/review_bridge/test_server.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/review_bridge/test_repository.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/review_bridge/test_service.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/review_bridge/test_auth.py`; create `/Users/macbook/Data/reup-flow/services/localization/tests/workflows/test_revoice_caption.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/activities/test_revoice_caption.py`, `/Users/macbook/Data/reup-flow/services/localization/tests/integration/test_review_roundtrip.py` và `/Users/macbook/Data/reup-flow/scripts/tests/review-studio.test.mjs`.

## API và canonical state

Bridge session API gồm GET project theo aweme/generation, PUT cue với expectedRevision/headETag, GET SRT, POST revoice với requestId/cueId/baseRevision, GET job status. Identity từ numeric run params + session grant, không nhận arbitrary object key/path từ browser. Giới hạn text/body/queued jobs, trả 409 kèm revision hiện tại khi stale. Không cần thêm editor video hoặc split/merge UI; SRT import nếu có chỉ nhận mapping không mơ hồ từ sidecar, không dùng số cue làm ID.

Caption đã lưu mới nhất (draft nếu audio chưa đồng bộ) hiện trong preview và xuất SRT; audio active giữ spoken text/timing/clip riêng. Sửa caption không tự TTS và không mất câu khi provider lỗi. Blank caption không tạo SRT cue nhưng hàng review vẫn tồn tại. Sửa timing validate 0 <= start < end <= duration, reject overlap với neighbor; nếu audio cũ không vừa slot mới thì giữ placement audio cũ và hiện mismatch, không cắt tail hay di chuyển các câu sau.

## Save/revoice commit protocol

1. Materialize từ durable HEAD của Phase 3. Local current pointer chỉ là cache; cold-open/xóa cache vẫn lấy được revision đã commit. Local working projection phục vụ Studio; file source/override edits chưa import không được ghi đè.
2. Save merge visual theo stable cue ID. So sánh fingerprint các file Studio trước/sau read, lock và HEAD ETag; Studio sửa song song -> báo conflict/retry merge, không overwrite. Chuẩn bị asset/source/manifest create-only, CAS HEAD, sau đó cập nhật local projection/pointer. Crash sau CAS trước local update phục hồi từ HEAD. Không tự delete orphan trong incident.
3. POST revoice dùng idempotency requestId gắn project/cue/baseRevision/text/timing/voice hash. Bridge start standalone RevoiceCaptionWorkflow; AlreadyStarted/retry/lost HTTP response attach workflow đang chạy hoặc đọc completed result, không tạo workflow mới để né conflict.
4. Workflow chỉ truyền refs qua Temporal. TTS activity trên tts queue dùng Synthesizer.clip()/TtsByProvider; đo duration, media activity áp atempo thực sự khi 1 < duration/slot <=1.15. Quá ngưỡng/lỗi giữ clip cũ, draft mismatch; câu chưa có clip vẫn omitted. Không tự viết ngắn lại câu người dùng đã sửa.
5. Thành công: thay đúng cue clip, rebuild **derived mixed audio** từ clip mới + các clip cũ + BGM (media queue, không TTS lại câu khác), giữ source video mute và chỉ bật một mixed track. Check duration và placements, rồi io commit activity của workflow ghi manifest + HEAD CAS ở baseRevision. Duplicate completed commit trả revision đã commit; stale result không ghi đè HEAD mới.
6. Worker io commit lưu result ref và commit outcome bền vững theo requestId; retry sau CAS trả cùng revision. Bridge restart/poll chỉ đọc outcome/HEAD, không commit HEAD lần hai. Hai request khác nhau cùng base chỉ một HEAD thắng; loser giữ candidate và báo conflict, không tự rebase/activate giọng sai text.
7. Cue/reason là untrusted data: dùng JSON encoder, escape script closing khi buộc nhúng; UI chỉ textContent/value, không innerHTML/eval. Companion CSP default-src self, script-src self hoặc hash, object-src none, frame-ancestors none; không unsafe-inline. Studio có policy phù hợp runtime đã chứng minh, không vô hiệu CSP để né lỗi. Test payload HTML/JS/SRT giữ literal, không phát sinh revoice/save ngoài ý muốn.
8. Extension trace dùng VoiceStudio và Vbee đã có qua TtsByProvider/VoiceResolver: contract clip ref + measured duration giữ nguyên, resolve voice/caching nằm worker; không thêm registry hay tự chuyển provider. Vbee pipeline cũ không bị đổi output.

## Verification

```bash
cd /Users/macbook/Data/reup-flow
node --test scripts/tests/review-studio.test.mjs
cd /Users/macbook/Data/reup-flow/services/localization
uv run pytest -q tests/review_bridge tests/workflows/test_revoice_caption.py tests/activities/test_revoice_caption.py
uv run pytest -q tests/integration/test_review_roundtrip.py -m live
uv run ruff check src tests
```

Các test cần Studio/provider/backend thật đánh dấu live (pyproject đã có marker); lệnh -m live là gate riêng với prerequisite rõ ràng, không tính suite mặc định bỏ live là đã pass integration. Test live Studio+bridge riêng khi đủ prerequisite; unit tests không được coi là backend/provider smoke. Matrix: host launcher/reuse/stop; TTL/replay grant/CSRF/path escape; save -> reopen -> early text word-count change -> later style giữ đúng ID; caption mới/SRT mới với audio cũ có mismatch; actual atempo <=1.15; overflow/failure; fill blank; concurrent save/style/revoice; lost response trước/sau workflow completion; crash ở manifest/HEAD/local update; cold-open không local cache; only-selected cue gets new TTS. Provider có thể bị gọi lại khi crash trước cache commit như semantics hiện có, không hứa exactly-once billing.

## Acceptance / rollback

- [ ] Companion và Studio mở được bằng host launcher; socket/permission/session/payload tests pass; bridge không chứa provider/DB secrets.
- [ ] Save/style/SRT cold-open đúng từ HEAD, stale request không overwrite, XSS payload literal.
- [ ] Revoice một cue làm thay đổi audio đang nghe qua mix mới; clip câu khác giữ nguyên, <=1.15x thực thi thật, lỗi/tràn giữ giọng cũ và caption mới.
- [ ] Lost response/AlreadyStarted/crash có đường phục hồi; không reset HEAD hoặc lặp provider có thể tránh được.

Rollback dừng tạo request mới ở bridge, giữ worker xử lý job đã start; CAS HEAD về manifest cũ bằng **ETag hiện tại**, sau đó rematerialize local. Dừng launcher/Studio đúng PID do mình khởi chạy. Không rollback bằng chỉ sửa local pointer hoặc xóa revision.

## Execution evidence — 2026-11-10

Companion/API/auth, immutable save/projection recovery, host launcher and durable one-cue
revoice are implemented. Focused tests include actual ffmpeg/Temporal and browser playback;
see [execution report](../reports/cook-261110-0620-hyperframes-phase-04.md).
The launcher refuses the unsafe native Studio boundary before loading real review data.
The live smoke was explicitly skipped for missing prerequisites. Acceptance remains open
until native Studio security/close-reopen/override behavior and the dedicated live smoke
pass; the unchecked acceptance items are intentional. Delivery remains disabled, and
Phase 05 migration/rollout was not executed by this invocation.

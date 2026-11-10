---
phase: 1
title: "Chứng minh tương thích Studio"
status: pending
priority: P1
effort: 1.5d
dependencies: []
---

# Phase 1: Chứng minh tương thích Studio

<!-- Updated: Red Team Review 2026-11-09 - host launcher/storage CAS capability and untrusted-output gates -->

## Goal

Khóa bằng chứng thực nghiệm cho pin `hyperframes@0.8.77`, project local, refresh/save visual, SRT, stable style mapping, source mask và seam revoice trước khi đổi production contract.

## Context / stop gate

- Source inspection cho thấy caption overrides chỉ persist một phần style, không text/start/end và không có public UI extension API ([report](../reports/researcher-261108-2224-hyperframes-integration.md)). Companion là canonical editor; không tuyên bố Studio có button/text editor native.
- Repo yêu cầu Node >=22 ([`package.json:5`](/Users/macbook/Data/reup-flow/package.json:5)). Chỉ dùng command/flag xuất hiện trong help của đúng pin; không dùng `--preserve-cues` nếu `0.8.77` không có.
- Nếu `0.8.77` không đạt, thử một version mới được pin chính xác và ghi installed-vs-expected/evidence. Chỉ stop/replan nếu full scope buộc đổi UX, maintained fork/provider hoặc không thể giao; không cắt text/timing/style/revoice.

## Files

- Create `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/package.json`, `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/pnpm-lock.yaml`, `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/pnpm-workspace.yaml` và `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/verify-roundtrip.mjs` — exact pin/help/fixture gate.
- Create `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/project/index.html`, `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/project/captions.js`, `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/project/styles.css` — plain HyperFrames composition với stable cue element IDs, video/audio/source mask; không giả định React/TSX build route.
- Create `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/fixtures/cues.json`, `/Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review/fixtures/captions.srt` — blank cue, earlier-word-count change, later styled cue.
- Create `/Users/macbook/Data/reup-flow/services/localization/tests/spikes/test_hyperframes_review_contract.py` — path/auth/revoice adapter tests without provider.

## Data flow to prove

`cues.json (stable cueId)` → generator updates stable-ID timed elements/project source → Studio loopback refresh/visual edit → adapter merges whitelisted visual changes into new cue revision → SRT exports text/timing only. SRT sequence/`wN`/wordIndex never becomes identity. Chosen representation defaults to stable-ID timed elements; if Studio cannot edit requested visuals on it, the gate must prove an explicit stable-ID adapter before continuing.

## Steps

1. Pin package/lock/checksum/gitHead. Root pnpm-workspace.yaml chỉ nhận services/*, nên spike là standalone nested workspace với pnpm-workspace.yaml riêng (packages: ["."]) và lockfile riêng. Tạo lock lần đầu, rồi kiểm chứng frozen install; launcher dùng đúng package pin đã kiểm chứng. Run help from the pin and record supported commands.
2. Start Studio on deterministic loopback port with observable PID; refuse port collision instead of incrementing. Open real fixture, close/reopen, stop cleanly.
3. Verify text/timing are companion-owned and which visual fields Studio saves. Edit words in an early cue, reload, and prove a later cue's style remains attached to its stable ID rather than shifted `wordIndex/wN`.
4. Prove canonical regeneration preserves Studio visual edits. No full source rewrite may wipe style overrides.
5. Import/export SRT; prove sequence/custom ID loss and detect ambiguous remap instead of guessing. Blank review row stays in sidecar, not SRT.
6. Render cover full-width with height from detected source subtitle region. Timing follows **source** subtitle cues even when Vietnamese is unresolved; absent region means no cover. Caption default yellow/black outline; no forced two-line height. Approve blur/opacity/font by screenshot/reference.
7. Fixture bridge writes only bind-mounted project root, rejects absolute/`..`/symlink escape, non-loopback Host/Origin. No credential in project bytes.
8. Contract-test custom revoice: normal clip activates; `slot < duration ≤1.15×slot` actually invokes `atempo`; >1.15×/failure retains active clip and draft mismatch.
9. Kiểm chứng startup từ macOS host launcher ở Phase 4: exact pin/package lock, argv arrays, host bind-mount mapping, port/PID/reuse/stop; container không spawn host CLI và không mount Docker socket. Thử bad Origin/Host vào cả Studio và bridge.
10. Probe MinIO/S3 conditional create, multipart completion và HEAD If-Match trên bucket test với identity scope review như Phase 3/4; record backend/SDK versions. Không coi check-then-write hoặc destination copy overwrite là CAS. Smoke này được chạy khi có test backend, không dùng production bucket để dựng fixture.
11. Fixture cue/reason gồm HTML/script payload và thay đổi style đồng thời. Prove literal rendering, standard JSON encoding, CSP phù hợp và stable style merge; text mới/SRT mới vẫn giữ khi audio cũ chưa đồng bộ.

## Verification

```bash
cd /Users/macbook/Data/reup-flow/services/localization/spikes/hyperframes-review
pnpm install --frozen-lockfile
pnpm exec hyperframes --help
pnpm test
cd /Users/macbook/Data/reup-flow/services/localization
uv run pytest -q tests/spikes/test_hyperframes_review_contract.py
```

Layout preflight runs in project image/runtime with Pillow `raqm`. Workspace baseline has 35 construction failures because `raqm` is absent; report prerequisite, never weaken/skip assertions to manufacture green.

## Acceptance

- [ ] Pin/commands reproducible; project opens/refreshes/reopens on tracked loopback Studio.
- [x] Stable style survives earlier text token-count change and canonical regenerate.
- [x] SRT boundaries/content/timing round-trip; ambiguous identity rejected, blank absent from SRT but present in review sidecar.
- [ ] Visual source mask/caption match reference; adapter/revoice unit gate pass. Live Studio/origin/storage CAS probes có evidence riêng; không nhầm unit test với real provider acceptance.

## Risks / rollback

| Risk | L×I | Stop signal | Mitigation / rollback |
|---|---|---|---|
| Pin lacks required public behavior | M×H | fixture cannot open/refresh | Preserve spike; test a newer exact pin; stop only if delivery boundary/UX must change. |
| Style drifts by word index | H×H | later style moves after edit | Stable cue elements/adapter; reject representation. |
| Internal format changes | H×M | fixture diff/schema error | Exact lock + adapter only; no undocumented Studio endpoint. |

Phase 2 bắt đầu khi technical gate pass và screenshot comparison với ảnh tham chiếu đã được ghi lại; không tạo thêm approval checkpoint cho các giá trị visual vẫn chỉnh được trong Studio.


## Execution evidence — 2026-11-09

[Implementation and live evidence](../reports/cook-261109-2018-hyperframes-phase-01.md).
Node contracts: 3 passed; adapter contracts: 23 passed; image layout preflight: 52 passed;
frozen install, Ruff, CLI runtime/layout/contrast and isolated review-only MinIO CAS pass.
The user reference was supplied and compared; editable fixture uses italic yellow/black
text and the required full-width source-timed band. This is approximate styling, not
font identification or live OCR verification.

The phase remains unaccepted. Both 0.8.77 and fallback probe 0.9.26 accepted bad Host
on the read-only project-list endpoint. Bad Origin also returned 200; a browser mutation
exploit was not demonstrated. Fixture bridge rejects both with 403. Native style save
and refresh were verified; final browser reload timed out, leaving full close/reopen
verification open. Host launcher has foreground PID/stop/collision behavior, not
cross-session reuse. Do not advance to Phase 2 or claim the live boundary gate passed.

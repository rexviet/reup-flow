# HyperFrames Phase 1 execution

Date: 2026-11-09, Asia/Saigon. Scope: `phase-01-start.md --auto` only.

The compatibility fixture is implemented. The phase is **not accepted**: both Studio
versions tested accept a non-loopback Host on the project-list endpoint. Production
contracts remain unchanged, and Phase 2 was not started. Existing checkout changes
were preserved; no commit or publication was performed.

## Reproducibility and behavior

- Selected `hyperframes@0.8.77` has an isolated nested pnpm workspace and frozen
  lockfile. Registry integrity, SHA-1 and gitHead are in
  [`provenance.json`](../../services/localization/spikes/hyperframes-review/provenance.json).
  Node 22.22.0/pnpm 11.23.0 were invoked through `npm exec` because shell defaults
  varied between Node 20/22.12 and no pnpm. Global defaults were not changed.
- Exact help confirmed `preview --port --foreground --no-open` and
  `transcribe --to srt --preserve-cues`. No native `captions` command was assumed.
  A `0.9.26` fallback probe was run separately; it was not adopted or substituted
  for the selected package. The fallback dependency tree was temporary npm cache,
  not a second committed lockfile.
- Real local media opened in Studio 0.8.77. Its native inspector saved `cue-later`
  font size from 36px to 42px into HTML. The adapter captured that whitelisted style
  by stable element ID, changed the earlier cue's word count, regenerated canonical
  source, and native Chrome showed the later cue still at 42px after refresh.
  The artifact `data/diagnostics/hyperframes-review/evidence/studio-saved-0.8.77.html`
  records the actual saved source. Generic timed elements work; no caption word-index
  persistence or native revoice button is claimed. Text-field interaction did not
  establish native text persistence; companion remains canonical for text/timing.
- Studio was stopped and restarted on the same port between the selected and fallback
  probes, then started with the host launcher. A final native-browser reload attempt
  timed out, so a complete close/reopen browser persistence gate is still unproven.
- Canonical SRT preserves tested boundaries, Unicode, literal markup-like content,
  multiline text and spaces. Blank cue/reason remains in JSON, absent from SRT.
  Sequence numbers and custom IDs are not identity; ambiguous, changed, missing or
  duplicate timing mappings are rejected. Native CLI import/export kept ordinary
  cue boundaries but stripped the literal `<img…>`/`</script>` payload to `&`.
  Use the companion adapter for literal canonical exchange.
- Revoice contracts use real WAV files and ffprobe/ffmpeg. Normal candidates activate;
  1.10× and 1.15× candidates actually run `atempo` and fit the slot; 1.16×, missing
  candidates and a real ffmpeg overwrite refusal preserve old audio and draft mismatch.
  Provider calls, derived production mixes, Temporal and durable revisions belong to later phases.

## Visual and CSP evidence

The user supplied the reference at `/var/folders/35/ck43ksxd6bs7cvcmvdzrhl3c0000gn/T/orca-paste-1794230779903-3378d1f6-8f5e-4645-b4f9-0e777c9d37b8.png`.
A local copy is `data/diagnostics/hyperframes-review/evidence/user-reference.png`.

Comparison: yellow fill, black outline and italic text are represented with Georgia
24px; this is an approximate font match, not an identified copy of the reference font.
The reference has local blurred patches; the accepted design explicitly requires a
full-width band. The fixture therefore uses full width, 70% black and 8px backdrop
blur. Its 86px fixture source region controls height independently of caption wrapping.
This proves composition behavior for supplied region data, not live OCR detection or
pixel equivalence on a different source frame. Source-cue times drive masks: the 2.8s
frame retains the mask with no Vietnamese caption. Removing the region removes all masks.
These are editable fixture values, not newly frozen product configuration.

Initial CLI snapshots were invalid: CSP blocked the inlined runtime, so all timed cues
appeared together. Those frames are not acceptance evidence. The fix trusts five exact
script hashes verified against the pinned registry tarball and the owned captions script;
see [`runtime-hashes.json`](../../services/localization/spikes/hyperframes-review/runtime-hashes.json).
No script `unsafe-inline` or arbitrary Studio script trust was added. The same check
passed after regeneration and visual changes. Valid frames are under
`data/diagnostics/hyperframes-review/evidence/verified/` and `reference-comparison/`.
Payload text visibly remains literal, with no injected image/script element.

## Live boundaries and storage

| Surface | Probe | Result |
|---|---|---|
| Studio 0.8.77 | Listener | PID 68596 initially; launcher PID 76225 later; `127.0.0.1:18440` only |
| Studio 0.9.26 | Listener | PID 74002; `127.0.0.1:18440` only |
| Both Studio versions | `GET /api/projects`, Host `evil.test` | **200; strict rejection gate unmet** |
| Both Studio versions | Same GET, Origin `https://evil.test` | 200; no ACAO observed on newer probe; no mutation exploit demonstrated |
| Fixture bridge | PID 79923, `127.0.0.1:18441` | Bad Host/Origin 403; valid patch 200; stale revision 409 |
| Isolated MinIO | Temporary loopback 18443, RAM-only bucket | Conditional create, HEAD If-Match, HEAD-object update CAS and multipart completion pass |
| MinIO review identity | Write outside `review/` | 403 |

MinIO was `RELEASE.2026-10-23T20-06-51Z`, pinned image digest in `probe-storage.py`;
boto3/botocore 1.43.118. Duplicate create, stale HEAD/PUT and duplicate multipart
completion all returned 412. The smoke used generated temporary admin credentials only
to set up the isolated bucket/principal; all actual probes used a review-only identity.
No production bucket, database, application secret or provider was touched. This is not
proof that a future production storage policy has been provisioned.

The host launcher verifies the exact package, maps resolved bind-mount paths and uses
argv arrays; occupied ports are refused. Foreground PID ownership and signal forwarding
were exercised. Cross-session reuse/status persistence remains unimplemented in this
fixture. No container spawned a host CLI or received a Docker socket. The fixture bridge
is serial and revision-checked, not a claim of multi-file transactional persistence.

## Verification

| Check | Final result |
|---|---|
| Frozen nested pnpm install | Pass |
| Node fixture/launcher contracts | 3 passed |
| Python adapter contracts | 23 passed |
| Ruff on adapter, storage probe and Python tests | Pass |
| HyperFrames check | Pass: runtime 0 errors, layout 0 issues across 9 samples, contrast 4/4 |
| HyperFrames lint | 0 errors; 1 expected dense-track warning for four source masks |
| Layout preflight in test image with source/tests/contracts/assets mounted read-only | 52 passed |
| Host Pillow | `raqm=False`; image Pillow `raqm=True` |
| Storage and live bridge probes | Pass as described above |

Initial Node failure was a macOS `/var` versus `/private/var` test expectation; the
assertion now compares canonical paths without weakening containment. Initial container
layout run had 32 failures because the harness omitted `/assets`; the same assertions
passed after mounting the actual font read-only. No tests were skipped or weakened.

Simplifier performed scoped fixes; counsel reviewed failed-verification repairs.
Inline code review covered path confinement, encoding, argv handling, old-audio retention,
CAS outcomes and accepted UX. Remaining fixture limits are the explicitly deferred durable
storage/session concerns above. All temporary Studio/bridge/MinIO services were stopped.

## Remaining gate / next action

The strict Studio boundary and full browser close/reopen gates are still open. A bridge
or front proxy alone cannot protect the independently browser-reachable Studio listener.
Before advancing, resolve an upstream-supported Host/Origin boundary or explicitly replan
an isolation/maintained integration approach while preserving the accepted companion UX.
Do not silently introduce a Studio fork or treat unit tests as live Studio acceptance.

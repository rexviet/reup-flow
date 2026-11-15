# Native Studio full-video handoff

Canary: `7689288513854595771`, generation 2, 2026-11-10.

- Native Chrome verification: duration 03:09, populated video/mask/caption/mixed-audio timeline, enabled Play, playback advanced to 00:15. Yellow captions with black outlines appear individually. Studio left paused for human review.
- Pin remains 0.8.77. Repository-owned HTTP preload forces loopback 3002 and rejects invalid Host/Origin, cross-site requests and websocket upgrades before vendor handlers. Empty and real project probes remain mandatory; unwrapped vendor behavior is unchanged.
- Fixed CSP blocking the exact local preview runtime. Generated stable native editor IDs avoid automatic source stamping. Projection excludes preview, waveform, thumbnail and stamp caches while tracking source and overrides.
- Complete local backup: ignored `backups/studio-recovery-20261110/g2`. Recovery validated source against its immutable baseline and preserved visual properties. Durable CAS HEAD: `b89afbea88e24b359761e98059ad0833`. Captions/timing unchanged; old revision history retained.
- Dedicated review identity provisioned with explicit user permission. Real HEAD read/scoped list succeeded; reads outside review denied. No credential values recorded.
- Focused Python tests: 36 passed. Node launcher/boundary tests: 6 passed. Scoped Ruff passed.
- Managed Studio PID 39490 on loopback 3002, project `data/review/7689288513854595771/g2`. Bridge on loopback 3310. Stop Studio using `node scripts/review-studio.mjs stop` under Node 22.13+; verified with Node 24.
- Companion exposes 38 cues and revoice controls. This run did not click its revoice button; earlier backend/provider verification remains separate. Native style persistence and broader rollout acceptance remain open. Starter default remains legacy.

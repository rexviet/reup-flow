# Native Studio export repair

The native exporter could not initialize `window.__hf.seek` or duration. Preview
playback alone did not prove the export path worked. Actual producer diagnostics
showed CSP blocking three trusted injected scripts: the pinned runtime with its
injection newline, the render-mode setup and the player-to-engine bridge.

The saved CSP trusted the preview runtime's bytes. Producer 0.8.77 injects those
same bytes plus one newline, yielding a different SHA-256. The new exact hashes
were measured from the pinned CLI's served export HTML and checked against
`fileServer.ts` at the provenance gitHead. Runtime equality with preview plus one
newline was asserted. Render-mode hashes cover the observed Studio FPS choices
24/30/60 with default render-seek configuration. Script permissions remain exact
hashes; no unsafe-inline, generic self script source or remote CDN was added.
The compiler's embedded font/image data URIs are permitted as non-script resources.

The initial check also exposed our competing standalone media player script and
requestAnimationFrame visibility loop, plus duplicate root compositions from
immutable hashed HTML copies. The generator now leaves media/timing to HyperFrames.
Materializers retain HTML dependency bytes as `.html.blob`, including existing
checksum-verified copies; modified copies and symlinks are refused. Human named
Studio files and immutable S3 object keys are unchanged.

Before changes, the current g3 projection was copied to
`backups/review-g3-export-20261110-2136/g3`. Current native visual edits were verified
against durable source, captured and retained through CAS. Caption tuples and mixed
audio were asserted equal. Current g3 revision is `b68524d5d25946dc818457ec46cd2979`.
The newly completed extension video 7691886043717039013/g1 was also migrated to
`dbdac506329542d1a4cb227b02ff5ddc`, preserving its captions and audio. No old video
was reprocessed, workflow mode rewritten, DB changed, or S3 output published.

## Verification

- Forty focused project/bridge tests passed; full Python Ruff passed.
- Two regression tests cover one-root cold materialization, retained original bytes,
  native media ownership and refusal to rename modified dependency copies.
- Scoped simplifier review found no additional changes needed; its 22 affected tests,
  Ruff and whitespace check passed.
- Pinned HyperFrames check passed: lint/runtime/motion zero errors/warnings,
  layout zero issues across nine samples, three of three contrast checks passed.
- The first real Studio retry still failed; a separate CLI debug render exposed the
  exact producer CSP hashes. That failure was not treated as a successful export.
- The next actual Studio export `g3_2026-11-10_21-51-37.mp4` passed runtime readiness
  and was observed capturing frame 2999 of 5690 with four workers. Final file
  verification is recorded below after completion.

The auto-open LaunchAgent was paused during maintenance and restored. Current
managed Studio PID is 5832 on loopback port 3002 (launcher foreground session 7623).
The newly completed review waits behind the current Studio, preserving the user's
active review. Disposable CLI debug render exited after its recorded failure.

## Completed native export

Studio displayed an enabled Download button for the completed
`g3_2026-11-10_21-51-37.mp4`. The file is under the g3 projection's `renders/`
directory. ffprobe verified H.264 video at 1024×576, 30/1 fps, AAC audio,
189.666667-second duration and 69,715,996-byte size. A decoded frame at 141 seconds
shows the requested yellow/black caption and translucent white box. This proves a
real full native export, rather than only lint/preview readiness.

Final read-only independent review verified all five producer hashes byte-for-byte,
including literal FPS variants and the one-newline runtime difference. No concrete
concern remained for Studio's default seek settings and supported FPS choices.
Custom seek environment settings or future FPS options require hash revalidation.

Known translation/ASR gaps in the reviewed generation remain unchanged; export does
not certify translation quality or publish/approve the workflow.

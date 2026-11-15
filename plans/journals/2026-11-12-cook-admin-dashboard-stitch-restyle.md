# Admin dashboard: Stitch dark re-skin

Date: 2026-11-12 (Asia/Saigon). Branch: feat/localization-dubber-code-mode. Not committed.

## What changed

- `services/web` only; no API, hook or behavior change. Design tokens (colors, radii, font sizes, Inter and
  JetBrains Mono families) moved into `tailwind.config.js`; dark is the only theme (`<html class="dark">`).
- Fonts bundled via `@fontsource-variable/inter` and `@fontsource-variable/jetbrains-mono` (lockfile updated);
  icons are inline SVG (`components/Icon.tsx`). Nothing loads from a CDN at runtime.
- New shell (`components/AppShell.tsx`): top bar, left sidebar from `lg`, same links under the top bar on narrow screens.
- Restyled health row, urgent box, stage chips (plus an "all" chip), filters, bulk bar, pipeline table, download
  form, jobs, channels, confirm dialog, notices, and the detail page (players side by side, timeline tiles,
  review, cost/generations, history).
- Added: `StageBadge` (icon + text), `formatDuration` (list API has `duration_ms`), mismatch count in rows, health
  "last updated" from `generatedAt`.
- Tests: HealthBar class assertions now check token classes plus visible state words; new tests for AppShell,
  formatDuration/tone icons, the all-chip, duration and attention marker.

## Findings worth remembering

- An absolutely positioned `sr-only` span inside an `overflow-x-auto` table makes the page scroll sideways unless the
  scroller is `relative`. Fixed on the pipeline table.
- A grid with an implicit column grows to the min-content of an unbroken id; use `grid-cols-1` and `min-w-0`.
- The design's muted text color (#64748b) is under 4.5:1 on the layer-1 surface; `text-muted` was lightened to #8b9bb0.
- Docker serves the old bundle until the browser cache is bypassed (query string worked).

## Left out (no backend support)

Thumbnails, sidebar items other than the real routes, keyboard-shortcut bar, host machine stats, LLM cost estimate on
the selection bar, queue-priority checkbox, auto-refresh countdown, pagination beyond the existing prev/next, per-channel
sync interval, subtitle cue table (no cue data in the API), generation switcher (API has no per-generation detail view),
CF Tunnel / worker reload / settings buttons.

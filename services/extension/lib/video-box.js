'use strict';

/**
 * Where the video the user is watching is drawn, so content.js can pin the
 * button to ITS top-right corner instead of the window's. Pure geometry, so
 * the choice between Douyin's several <video> elements is unit tested.
 *
 * Douyin pages carry more than one: the playing one, a blurred full-width
 * backdrop behind it, preview thumbnails, and the next/previous feed items
 * stacked off-screen. The one being watched is the SMALLEST big-enough picture
 * that contains the centre of the window.
 */
const MIN_SIDE = 150;

/** The picture an `object-fit: contain` video draws inside its element's box. */
function containFit(rect, videoWidth, videoHeight) {
  if (!videoWidth || !videoHeight) return rect;
  const scale = Math.min(rect.width / videoWidth, rect.height / videoHeight);
  const width = videoWidth * scale;
  const height = videoHeight * scale;
  const left = rect.left + (rect.width - width) / 2;
  const top = rect.top + (rect.height - height) / 2;
  return { left, top, width, height, right: left + width, bottom: top + height };
}

/**
 * `candidates`: `[{ rect, videoWidth, videoHeight }]`. Returns the chosen
 * picture's box, clamped to the viewport, or null when none qualifies.
 */
function pickVideoBox(candidates, viewport) {
  const cx = viewport.width / 2;
  const cy = viewport.height / 2;
  let best = null;
  for (const c of candidates) {
    const fit = containFit(c.rect, c.videoWidth, c.videoHeight);
    if (fit.width < MIN_SIDE || fit.height < MIN_SIDE) continue;
    if (cx < fit.left || cx > fit.right || cy < fit.top || cy > fit.bottom) continue;
    if (best === null || fit.width * fit.height < best.width * best.height) best = fit;
  }
  if (best === null) return null;
  return {
    left: Math.max(best.left, 0),
    top: Math.max(best.top, 0),
    right: Math.min(best.right, viewport.width),
    bottom: Math.min(best.bottom, viewport.height),
  };
}

(() => {
  const root = typeof self !== 'undefined' ? self : globalThis;
  root.containFit = containFit;
  root.pickVideoBox = pickVideoBox;
})();
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { containFit, pickVideoBox };
}

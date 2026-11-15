'use strict';

const { containFit, pickVideoBox } = require('../lib/video-box');

const VIEWPORT = { width: 2000, height: 767 };
const rect = (left, top, width, height) => ({
  left, top, width, height, right: left + width, bottom: top + height,
});

describe('containFit', () => {
  test('letterboxes a 9:16 video inside a wider element, centred', () => {
    const fit = containFit(rect(0, 0, 2000, 745), 720, 1280);
    expect(fit.height).toBeCloseTo(745);
    expect(fit.width).toBeCloseTo(745 * (720 / 1280));
    expect(fit.left + fit.width / 2).toBeCloseTo(1000);
  });

  test('uses the element box as is when the video size is not known yet', () => {
    expect(containFit(rect(10, 20, 300, 400), 0, 0)).toEqual(rect(10, 20, 300, 400));
  });
});

describe('pickVideoBox', () => {
  const main = { rect: rect(791, 0, 418, 745), videoWidth: 720, videoHeight: 1280 };

  test('returns the displayed picture, not the wider element around it', () => {
    const wide = { rect: rect(0, 0, 2000, 745), videoWidth: 720, videoHeight: 1280 };
    const box = pickVideoBox([wide], VIEWPORT);
    expect(box.left).toBeGreaterThan(780);
    expect(box.right).toBeLessThan(1220);
  });

  test('prefers the smaller picture over a blurred full-width backdrop', () => {
    const backdrop = { rect: rect(0, 0, 2000, 745), videoWidth: 2000, videoHeight: 745 };
    expect(pickVideoBox([backdrop, main], VIEWPORT)).toMatchObject({ left: 791, right: 1209 });
  });

  test('ignores videos that do not contain the viewport centre (thumbnails, off-screen feed items)', () => {
    const thumb = { rect: rect(50, 100, 300, 400), videoWidth: 720, videoHeight: 1280 };
    const below = { rect: rect(791, 800, 418, 745), videoWidth: 720, videoHeight: 1280 };
    expect(pickVideoBox([thumb, below], VIEWPORT)).toBeNull();
  });

  test('ignores tiny videos', () => {
    const tiny = { rect: rect(950, 350, 100, 60), videoWidth: 100, videoHeight: 60 };
    expect(pickVideoBox([tiny], VIEWPORT)).toBeNull();
  });

  test('clamps a partly off-screen video to the viewport so the button stays reachable', () => {
    const tall = { rect: rect(791, -200, 418, 1200), videoWidth: 418, videoHeight: 1200 };
    const box = pickVideoBox([tall], VIEWPORT);
    expect(box.top).toBe(0);
    expect(box.bottom).toBe(767);
  });

  test('no candidates -> null', () => {
    expect(pickVideoBox([], VIEWPORT)).toBeNull();
  });
});

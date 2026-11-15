import { describe, expect, it } from 'vitest';
import { coverBox, roundHalfEven } from './cover-geometry';

// The same table as the localization geometry test: both sides must draw the same box.
// W×H | region (y,h) | offset | width | height | → top, height, width %
const CASES: Array<[string, number, number, { y: number; h: number } | null, number | null, number | null, number | null, number, number, number]> = [
  ['unset values keep the detected band', 720, 1280, { y: 1000, h: 120 }, null, null, null, 1000, 120, 60],
  ['offset moves the centre down, width widens', 720, 1280, { y: 1000, h: 120 }, 3, 100, null, 1038, 120, 100],
  ['height grows around the band centre', 720, 1280, { y: 1000, h: 120 }, null, null, 12, 983, 154, 60],
  ['a large offset is clamped to the frame', 720, 1280, { y: 1000, h: 120 }, 50, null, null, 1160, 120, 60],
  ['no band falls back to 0.8H and 0.14H', 720, 1280, null, null, 80, null, 1024, 179, 80],
];

describe('coverBox', () => {
  it.each(CASES)('%s', (_label, W, H, region, offsetPct, widthPct, heightPct, top, height, width) => {
    expect(coverBox({ frameWidth: W, frameHeight: H, region, offsetPct, widthPct, heightPct })).toEqual({ top, height, widthPct: width });
  });

  it('never puts the box above the frame', () => {
    expect(coverBox({ frameWidth: 720, frameHeight: 1280, region: { y: 10, h: 120 }, offsetPct: -50, widthPct: null, heightPct: null }).top).toBe(0);
  });
});

describe('rounding, as the worker does it', () => {
  it('rounds a centre that falls on a half up, like the worker _half_up', () => {
    // Base 121 px tall at y=1000 (centre 1060.5), height 12 % of 1280 = 154: top = 1060.5 − 77 = 983.5 → 984.
    expect(coverBox({ frameWidth: 720, frameHeight: 1280, region: { y: 1000, h: 121 }, offsetPct: null, widthPct: null, heightPct: 12 }).top).toBe(984);
    expect(coverBox({ frameWidth: 720, frameHeight: 1280, region: { y: 999, h: 121 }, offsetPct: null, widthPct: null, heightPct: 12 }).top).toBe(983);
  });

  it('roundHalfEven rounds halves to the even neighbour, like Python round (the fallback box)', () => {
    expect([0.5, 1.5, 2.5, 982.5, 983.5, -0.5, -1.5].map(roundHalfEven)).toEqual([0, 2, 2, 982, 984, 0, -2]);
    expect([1.4, 1.6, 179.20000000000002].map(roundHalfEven)).toEqual([1, 2, 179]);
  });
});

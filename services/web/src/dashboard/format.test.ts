import { describe, expect, it } from 'vitest';
import { formatDuration, STAGE_TONES, TONE_ICONS } from './format';
import { STAGES } from '../api/types';

describe('formatDuration', () => {
  it('formats m:ss and h:mm:ss', () => {
    expect(formatDuration(12_000)).toBe('0:12');
    expect(formatDuration(95_000)).toBe('1:35');
    expect(formatDuration(3_725_000)).toBe('1:02:05');
  });
  it('returns null when the length is unknown or invalid', () => {
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(undefined)).toBeNull();
    expect(formatDuration(-1)).toBeNull();
    expect(formatDuration(Number.NaN)).toBeNull();
  });
});

describe('stage tones', () => {
  it('every stage has a tone and every tone an icon, so status is never colour-only', () => {
    for (const stage of STAGES) expect(TONE_ICONS[STAGE_TONES[stage]]).toBeTruthy();
  });
});

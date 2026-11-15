/**
 * `cleanDisplayName` -- the one sanitiser for every Douyin-supplied name that
 * reaches the dashboard and the database (a 合集 `mix_name`, an author
 * `nickname`). Douyin lets creators put anything in those fields, including
 * bidi overrides and zero-width characters that make two different names look
 * identical or reorder the text around them in the UI.
 *
 * Never used for file names: those are built from ids only.
 */
import { cleanDisplayName, DISPLAY_NAME_MAX_CODE_POINTS } from '../../src/parser/display-name';

const FALLBACK = '7695338683825653823';

describe('cleanDisplayName', () => {
  it('returns an ordinary name unchanged', () => {
    expect(cleanDisplayName('成名的代价', FALLBACK)).toBe('成名的代价');
    expect(cleanDisplayName('IM-薛珍珠', FALLBACK)).toBe('IM-薛珍珠');
  });

  it('normalises to NFC, so a decomposed and a composed name compare equal', () => {
    const decomposed = 'Việt'; // e + combining circumflex + combining dot below
    expect(decomposed).not.toBe('Việt');
    expect(cleanDisplayName(decomposed, FALLBACK)).toBe('Việt');
  });

  it('returns NFC even when a stripped character sat between a letter and its combining mark', () => {
    expect(cleanDisplayName('e\u200B\u0302', FALLBACK)).toBe('\u00EA');
  });

  it('strips Cf characters: bidi controls and zero-width characters', () => {
    // U+202E RIGHT-TO-LEFT OVERRIDE, U+200B ZERO WIDTH SPACE, U+200D ZWJ,
    // U+FEFF BOM, U+2066 LEFT-TO-RIGHT ISOLATE.
    expect(cleanDisplayName('a‮b​c‍d﻿e⁦f', FALLBACK)).toBe('abcdef');
  });

  it('strips Cc control characters that are not whitespace', () => {
    expect(cleanDisplayName('a\u0000b\u0007c\u007Fd\u009Fe', FALLBACK)).toBe('abcde');
  });

  it('collapses whitespace runs, including newlines and tabs, into one space and trims', () => {
    // A newline is Cc too; it must become a separator, not glue two words together.
    expect(cleanDisplayName('  成名\n\n的\t代价  ', FALLBACK)).toBe('成名 的 代价');
    expect(cleanDisplayName('a　 b', FALLBACK)).toBe('a b');
  });

  it('does not leave a double space where a stripped character sat between spaces', () => {
    expect(cleanDisplayName('a ​ b', FALLBACK)).toBe('a b');
    expect(cleanDisplayName('a \u0000 b', FALLBACK)).toBe('a b');
  });

  it('cuts a 500-character name to 200 code points', () => {
    const cleaned = cleanDisplayName('名'.repeat(500), FALLBACK);
    expect(DISPLAY_NAME_MAX_CODE_POINTS).toBe(200);
    expect(Array.from(cleaned)).toHaveLength(200);
    expect(cleaned).toBe('名'.repeat(200));
  });

  it('counts code points, not UTF-16 units, so an astral character is never split', () => {
    const cleaned = cleanDisplayName('😀'.repeat(300), FALLBACK);
    expect(Array.from(cleaned)).toHaveLength(200);
    expect(cleaned).toBe('😀'.repeat(200));
  });

  it('does not end on a space after cutting', () => {
    const cleaned = cleanDisplayName(`${'a'.repeat(199)} bcd`, FALLBACK);
    expect(cleaned).toBe('a'.repeat(199));
  });

  it('uses the fallback when nothing is left after cleaning', () => {
    expect(cleanDisplayName('​‍‮﻿', FALLBACK)).toBe(FALLBACK);
    expect(cleanDisplayName('   \n\t ', FALLBACK)).toBe(FALLBACK);
    expect(cleanDisplayName('', FALLBACK)).toBe(FALLBACK);
  });

  it('uses the fallback when the raw value is not a string', () => {
    expect(cleanDisplayName(undefined, FALLBACK)).toBe(FALLBACK);
    expect(cleanDisplayName(null, FALLBACK)).toBe(FALLBACK);
    expect(cleanDisplayName(42, FALLBACK)).toBe(FALLBACK);
  });
});

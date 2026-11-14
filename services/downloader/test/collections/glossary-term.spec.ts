import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { checkTerm, glossaryTermSchema } from '../../src/collections/glossary-term';

interface SharedCase {
  name: string;
  term_zh: string;
  term_vi: string;
  note: string | null;
  reason: string | null;
}

/**
 * The same file `services/localization/tests/agents/test_glossary.py` runs through
 * Python's `check_term`: every case must get the very same answer here.
 */
const CASES: SharedCase[] = JSON.parse(
  readFileSync(resolve(__dirname, '../../../../contracts/glossary-term-cases.json'), 'utf8'),
);

describe('checkTerm (the dashboard copy of check_term)', () => {
  it('reads a non-trivial shared case list', () => {
    expect(CASES.length).toBeGreaterThan(30);
    expect(CASES.some((c) => c.reason === null)).toBe(true);
    expect(CASES.some((c) => c.reason !== null)).toBe(true);
  });

  it.each(CASES.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expect(checkTerm(c.term_zh, c.term_vi, c.note)?.message ?? null).toBe(c.reason);
  });

  it('names the field the reason is about', () => {
    expect(checkTerm('苹果', 'Apple', 'xem shop-xyz.vn')?.field).toBe('note');
    expect(checkTerm('苹\n果', 'Apple', null)?.field).toBe('term_zh');
    expect(checkTerm('手机', 'iPhone 16', null)?.field).toBe('term_vi');
  });
});

describe('glossaryTermSchema (what PUT stores)', () => {
  it('strips surrounding whitespace the way the worker does, and a blank note becomes null', () => {
    const parsed = glossaryTermSchema.parse({ term_zh: ' 苹果　', term_vi: ' Apple ', note: '  ' });
    expect(parsed).toEqual({ term_zh: '苹果', term_vi: 'Apple', note: null });
  });

  // `check_term` itself applies no normalisation, which is what the shared cases pin down.
  // The worker's `GlossaryStore.upsert` does NFC its terms before storing them; whether the
  // dashboard should too is an open product decision, so this pins the current behaviour.
  it('stores the text without NFC normalisation', () => {
    const decomposed = 'Ca\u0301';
    expect(glossaryTermSchema.parse({ term_zh: '卡', term_vi: decomposed }).term_vi).toBe(decomposed);
  });

  it.each(CASES.filter((c) => c.reason !== null).map((c) => [c.name, c] as const))(
    'refuses "%s" with the same reason, on the field it names',
    (_name, c) => {
      const result = glossaryTermSchema.safeParse({ term_zh: c.term_zh, term_vi: c.term_vi, note: c.note });
      expect(result.success).toBe(false);
      if (result.success) return;
      expect(result.error.issues).toHaveLength(1);
      expect(result.error.issues[0].message).toBe(c.reason);
      expect(result.error.issues[0].path).toEqual([c.reason!.split(' ')[0]]);
    },
  );

  it('refuses a lone surrogate, which Postgres would store as U+FFFD instead of what was checked', () => {
    for (const body of [
      { term_zh: '苹\ud800果', term_vi: 'Apple' },
      { term_zh: '苹果', term_vi: 'Ap\udfffple' },
      { term_zh: '苹果', term_vi: 'Apple', note: '\ud83d' },
    ]) {
      expect(glossaryTermSchema.safeParse(body).success).toBe(false);
    }
    expect(glossaryTermSchema.safeParse({ term_zh: '表情', term_vi: '\ud83d\ude00' }).success).toBe(true);
  });

  it('rejects input that is not text before running the term rules', () => {
    expect(glossaryTermSchema.safeParse({ term_zh: '苹果', term_vi: 5 }).success).toBe(false);
    expect(glossaryTermSchema.safeParse({ term_zh: '苹果', term_vi: 'x'.repeat(5000) }).success).toBe(false);
    expect(glossaryTermSchema.safeParse({ term_zh: '苹果', term_vi: 'Apple', extra: 1 }).success).toBe(false);
  });
});

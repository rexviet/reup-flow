import { z } from 'zod';
import { PYTHON_UNASSIGNED_CLASS } from './python-unassigned';

/**
 * The dashboard's copy of `check_term` (services/localization/src/localization/
 * activities/glossary.py) and of the `find_contacts` it calls
 * (localization/text/safety.py). A term a person sets here reaches the
 * Localizer's prompt exactly like one the agent wrote, so it must pass the same
 * guard with the same answer. `contracts/glossary-term-cases.json` is run by the
 * jest and the pytest suite alike to keep the two copies equal.
 *
 * The regular expressions are written to match Python's `re` on `str`, which is
 * Unicode-aware where JavaScript is not:
 * - `\d` is any decimal digit (`\p{Nd}`), not only 0-9;
 * - `\s` and `str.strip()` use Python's whitespace set (`PY_SPACE`);
 * - `\b` is a boundary between Python word characters (letters, numbers, `_`);
 * - `[a-z]` under IGNORECASE also matches `İ`, `ı`, `ſ` and the Kelvin sign;
 * - lengths count code points, not UTF-16 units;
 * - a code point the worker's Unicode version leaves unassigned is no letter, digit or
 *   decomposition here either, even when Node's newer Unicode assigns it
 *   (`python-unassigned.ts`, checked against Python by `test_glossary.py`).
 */

export const TERM_MAX_CHARS = 64;
export const NOTE_MAX_CHARS = 200;

/** Python's `str.isspace()` set, which is also what `\s` matches in a `str` pattern. */
const PY_SPACE = '\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const S = `[${PY_SPACE}]`;
const NOT_S = `[^${PY_SPACE}]`;
const UNASSIGNED = `[${PYTHON_UNASSIGNED_CLASS}]`;
const WORD = `(?:(?!${UNASSIGNED})[\\p{L}\\p{N}_])`;
/** Python's `\\d` and `str.isdecimal()`. */
const ND = `(?:(?!${UNASSIGNED})\\p{Nd})`;
const B = `(?:(?<=${WORD})(?!${WORD})|(?<!${WORD})(?=${WORD}))`;
/** `[a-z]` under Python's IGNORECASE (NFKC keeps `İ` and `ı`; the other two fold anyway). */
const AZ = 'a-z\\u0130\\u0131\\u017f\\u212a';

// Python's `_CONTROL_RE`, control characters on purpose.
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\r\n\t\x00-\x1f\x7f]/u;
// Python's `_INVISIBLE`. An alternation, not a class: a class holding the zero-width joiner reads as one joined glyph.
const INVISIBLE_RE = /\u200b|\u200c|\u200d|\u2060|\ufeff|\u00ad/gu;
const STRIP_RE = new RegExp(`^${S}+|${S}+$`, 'gu');

/** A literal `i` under Python's IGNORECASE also matches `İ` and `ı`; JavaScript's does not. */
const TLDS = (
  'com|net|org|edu|gov|info|biz|vn|cn|hk|tw|jp|kr|sg|uk|us|ru|de|fr|io|co|me|tv|cc|ly|gl|' +
  'ai|app|dev|xyz|top|site|online|shop|store|link|vip|club|live|life|fun|pro|ink|wang|' +
  'icu|cyou|work|click|website|tech|page|bio|asia|vc|to|be|im'
).replace(/i/g, '[i\\u0130\\u0131]');

const URL_RE = new RegExp(`(?:https?|ftp)://${NOT_S}+|${B}www\\.${NOT_S}+`, 'giu');
const EMAIL_RE = new RegExp(`[${AZ}0-9._%+-]+@[${AZ}0-9-]+(?:\\.[${AZ}0-9-]+)+`, 'giu');
const DOMAIN_RE = new RegExp(
  `(?<![${AZ}0-9@-])(?:[${AZ}0-9](?:[${AZ}0-9-]*[${AZ}0-9])?\\.)+(?:${TLDS})(?![${AZ}0-9-])`,
  'giu',
);
const SPOKEN_DOMAIN_RE = new RegExp(`${B}[${AZ}0-9-]+${S}*(?:chấm|dot)${S}*(?:${TLDS})${B}`, 'giu');
const PHONE_RE = new RegExp(`(?<!${ND})(?<!${ND}[.,])\\+?\\(?${ND}(?:[${PY_SPACE}().-]?${ND}){6,}(?!${ND})`, 'gu');
const GROUPED_NUMBER_RE = new RegExp(`^[1-9]${ND}{0,2}(?:[.,]${ND}{3})+$`, 'u');
const DECIMAL_RE = new RegExp(`^${ND}$`, 'u');
const UNASSIGNED_SPLIT_RE = new RegExp(`(${UNASSIGNED})`, 'u');

/** `str.strip()` with no argument. */
export function pyStrip(text: string): string {
  return text.replace(STRIP_RE, '');
}

/**
 * NFKC (full-width letters and dots become ASCII), no invisible characters. A code point
 * the worker's Unicode leaves unassigned stays as it is and, being a starter there, blocks
 * composition and reordering across it; normalising the pieces between such code points
 * on their own does exactly that.
 */
function normalise(text: string): string {
  return text
    .split(UNASSIGNED_SPLIT_RE)
    .map((piece, i) => (i % 2 === 0 ? piece.normalize('NFKC') : piece))
    .join('')
    .replace(INVISIBLE_RE, '');
}

const codePoints = (text: string): number => [...text].length;
/** A lone surrogate: Postgres would store U+FFFD instead of the text that was checked. */
const LONE_SURROGATE_RE = /\p{Cs}/u;
const noLoneSurrogate = (v: string | null | undefined): boolean => v == null || !LONE_SURROGATE_RE.test(v);

/**
 * Whether `find_contacts(text)` would return anything. A match whose start lies
 * inside an earlier pattern's match is skipped there, but that earlier match was
 * itself found, so only the phone pattern's grouped-number exception matters.
 */
function hasContact(text: string): boolean {
  const clean = normalise(text);
  for (const pattern of [URL_RE, EMAIL_RE, DOMAIN_RE, SPOKEN_DOMAIN_RE]) {
    pattern.lastIndex = 0;
    if (pattern.test(clean)) return true;
  }
  for (const match of clean.matchAll(PHONE_RE)) {
    if (!GROUPED_NUMBER_RE.test(match[0])) return true;
  }
  return false;
}

function digitsOf(text: string): Set<string> {
  return new Set([...normalise(text)].filter((ch) => DECIMAL_RE.test(ch)));
}

export type TermField = 'term_zh' | 'term_vi' | 'note';

export interface TermProblem {
  field: TermField;
  /** Word for word what `check_term` returns. */
  message: string;
}

/** Why this term must not be stored, or null when it may be. Same order of checks as `check_term`. */
export function checkTerm(termZh: string, termVi: string, note: string | null): TermProblem | null {
  const fields: Array<[TermField, string]> = [
    ['term_zh', termZh],
    ['term_vi', termVi],
    ['note', note ?? ''],
  ];
  for (const [field, value] of fields) {
    if (CONTROL_RE.test(value)) return { field, message: `${field} must be a single line of plain text` };
  }
  if (pyStrip(termZh) === '') return { field: 'term_zh', message: 'term_zh is empty' };
  if (pyStrip(termVi) === '') return { field: 'term_vi', message: 'term_vi is empty' };
  if (codePoints(termZh) > TERM_MAX_CHARS) {
    return { field: 'term_zh', message: `term_zh is longer than ${TERM_MAX_CHARS} characters` };
  }
  if (codePoints(termVi) > TERM_MAX_CHARS) {
    return { field: 'term_vi', message: `term_vi is longer than ${TERM_MAX_CHARS} characters` };
  }
  if (note !== null && codePoints(note) > NOTE_MAX_CHARS) {
    return { field: 'note', message: `note is longer than ${NOTE_MAX_CHARS} characters` };
  }
  for (const [field, value] of fields) {
    if (hasContact(value)) {
      return { field, message: `${field} contains a URL, domain name, e-mail address or phone number` };
    }
  }
  const zhDigits = digitsOf(termZh);
  const extra = [...digitsOf(termVi)].filter((d) => !zhDigits.has(d));
  if (extra.length > 0) {
    // Python's `sorted` orders by code point; `<` on strings would order by UTF-16 unit.
    extra.sort((a, b) => a.codePointAt(0)! - b.codePointAt(0)!);
    return { field: 'term_vi', message: `term_vi contains digits that term_zh does not: ${extra.join('')}` };
  }
  return null;
}

/**
 * A term as `PUT /api/collections/:id/glossary/:term_zh` stores it. Surrounding
 * whitespace is stripped and a blank note becomes null, as the worker's
 * `GlossaryStore.upsert` does. The plan leaves out NFC normalisation; note that
 * `upsert` does apply NFC before `check_term` (which itself applies none), so a
 * term typed in decomposed form is stored differently from the agent's.
 *
 * The raw size caps only stop absurd input before the term rules run; the rules
 * themselves set the real limits.
 */
export const glossaryTermSchema = z
  .object({
    term_zh: z.string().max(1000),
    term_vi: z.string().max(1000),
    note: z.string().max(2000).nullish(),
  })
  .strict()
  .superRefine((v, ctx) => {
    for (const field of ['term_zh', 'term_vi', 'note'] as const) {
      if (!noLoneSurrogate(v[field])) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, path: [field], message: `${field} contains a lone surrogate` });
      }
    }
  })
  .transform((v) => {
    const note = v.note == null ? null : pyStrip(v.note);
    return { term_zh: pyStrip(v.term_zh), term_vi: pyStrip(v.term_vi), note: note === '' ? null : note };
  })
  .superRefine((v, ctx) => {
    const problem = checkTerm(v.term_zh, v.term_vi, v.note);
    if (problem) ctx.addIssue({ code: z.ZodIssueCode.custom, path: [problem.field], message: problem.message });
  });
export type GlossaryTerm = z.infer<typeof glossaryTermSchema>;

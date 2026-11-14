/**
 * The longest display name kept, in Unicode code points (not UTF-16 units, so
 * an emoji is never cut in half). Douyin's own limits are far shorter; this is
 * a ceiling against a hostile or broken payload, not a formatting rule.
 */
export const DISPLAY_NAME_MAX_CODE_POINTS = 200;

/** Format characters: bidi overrides and isolates, zero-width (incl. U+FEFF BOM). */
const FORMAT = /\p{Cf}/gu;
/** Control characters (NUL, BEL, DEL, C1...); whitespace among them is handled first. */
const CONTROL = /\p{Cc}/gu;
const WHITESPACE_RUN = /\s+/gu;

/**
 * A Douyin-supplied name (a 合集 `mix_name`, an author `nickname`) made safe to
 * show in the dashboard and store in the database.
 *
 * Douyin lets creators type anything into these fields. A right-to-left
 * override or a zero-width character makes two different names render the
 * same, or reorders the text around them, so they are removed rather than
 * escaped. Steps, in order:
 *
 *   1. Cf characters are removed. This comes before whitespace handling
 *      because JavaScript's `\s` matches U+FEFF (a Cf), which would otherwise
 *      survive as a visible space.
 *   2. Every whitespace run becomes one space BEFORE control characters are
 *      stripped: a newline is Cc, and stripping it first would glue two words
 *      together.
 *   3. Remaining Cc characters are removed, then whitespace collapsed again
 *      (a stripped character between two spaces leaves two spaces).
 *   4. NFC, so a decomposed and a composed spelling are the same string. Done
 *      AFTER stripping: a removed character that sat between a letter and its
 *      combining mark would otherwise leave the pair uncomposed.
 *   5. Cut to {@link DISPLAY_NAME_MAX_CODE_POINTS} code points and trimmed.
 *
 * When nothing is left -- or `raw` is not a string at all -- the caller's
 * `fallback` (an id) is returned, so a name is never an empty string.
 *
 * NOT a file-name sanitiser: path separators, `..` and reserved names pass
 * through untouched. File names are built from ids only.
 */
export function cleanDisplayName(raw: unknown, fallback: string): string {
  if (typeof raw !== 'string') return fallback;

  const cleaned = raw
    .replace(FORMAT, '')
    .replace(WHITESPACE_RUN, ' ')
    .replace(CONTROL, '')
    .replace(WHITESPACE_RUN, ' ')
    .normalize('NFC')
    .trim();

  const cut = Array.from(cleaned).slice(0, DISPLAY_NAME_MAX_CODE_POINTS).join('').trimEnd();
  return cut.length > 0 ? cut : fallback;
}

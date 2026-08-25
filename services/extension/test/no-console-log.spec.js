'use strict';

const fs = require('fs');
const path = require('path');

/**
 * phase-09 §9.5 / plan.md §0 nguyên tắc 3: the extension's own DevTools
 * console IS a log sink, and cookies must never reach any log sink. Rather
 * than trying to statically prove "this particular console.log call
 * happens to reference a cookie-shaped variable" (fragile, and easy to
 * fool by renaming), this bans `console.*` outright from every production
 * file in the package -- sw.js is the only file that ever sees a real
 * cookie value (via chrome.cookies.getAll), and nothing else needs a
 * console call badly enough to justify carving out an exception.
 *
 * This is a real, CI-runnable test -- not a one-off manual grep -- and its
 * own guard is proven by test/mutations/phase-09.sh, which inserts a
 * `console.log` into sw.js and requires this test to go red.
 */
const PRODUCTION_FILES = [
  'sw.js',
  'options.js',
  'lib/cookie-header.js',
  'lib/api-client.js',
  'lib/storage.js',
  'lib/push-cookie.js',
  'lib/options-view.js',
  'lib/backend-permission.js',
];

describe('no console.* call anywhere in production extension code', () => {
  for (const rel of PRODUCTION_FILES) {
    test(`${rel} contains no console.log/warn/error/info/debug/trace call`, () => {
      const full = path.join(__dirname, '..', rel);
      const contents = fs.readFileSync(full, 'utf8');
      expect(contents).not.toMatch(/console\s*\.\s*(log|warn|error|info|debug|trace)\s*\(/);
    });
  }
});

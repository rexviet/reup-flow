/**
 * Thrown when `platform` is not in `SUPPORTED_PLATFORMS` (cookie.service.ts).
 *
 * The parser fork reads exactly one literal Redis key,
 * `douyin:cookie:current` -- there is no platform-discovery mechanism on
 * that side (see crawlers/douyin/web/cookie_provider.py). A typo'd or
 * unsupported platform would therefore build a key nothing ever reads, and
 * the cookie would simply never take effect: no error, no log line,
 * nothing. This exception is what turns that silent failure into a loud
 * 400 at push time instead.
 *
 * The message carries only `platform`, never `push`'s other argument (the
 * cookie value) -- so this can never become the leak path `push`'s own
 * comment warns about.
 */
export class UnsupportedPlatformError extends Error {
  constructor(readonly platform: string) {
    super(
      `platform "${platform}" is not supported; the only cookie platform this ` +
        'service accepts is "douyin" (the parser fork reads a single literal Redis key)',
    );
    this.name = 'UnsupportedPlatformError';
  }
}

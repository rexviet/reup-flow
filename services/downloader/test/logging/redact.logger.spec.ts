import { RedactingLogger } from '../../src/logging/redact.logger';
import { redactor } from '../../src/redaction/redactor';

/**
 * IMPORTANT: this file never calls installStdioRedaction(). That overlap is
 * deliberate (plan §3.3): the stdio patch would ALSO scrub whatever
 * ConsoleLogger writes underneath, so a test that had both active could
 * stay green even if RedactingLogger's own redaction were deleted entirely
 * -- the stdio layer would silently cover for it. Leaving the patch
 * uninstalled here means every assertion below can only pass because
 * RedactingLogger itself redacted the value, which is the one thing this
 * file exists to prove.
 */
describe('RedactingLogger', () => {
  // The value MUST keep a marker from .gitleaks.toml's allowlist ("fake",
  // "sentinel", ...). That allowlist is what lets test fixtures carry a
  // cookie-shaped literal without turning the repo's secret scan red -- and
  // the reason it matches on the SECRET's text rather than on a path is so a
  // REAL cookie pasted into this file still trips the scan. Swap this for a
  // realistic-looking value and CI goes red, correctly.
  const COOKIE = 'sessionid=sentinel-fake-cookie-0123456789';
  /** Bare value, no `field=value` shape anywhere -- see the JSON-field test below. */
  const COOKIE_VALUE_ONLY = 'sentinel-fake-cookie-value-only-0123456789';
  let stdoutSpy: jest.SpyInstance;
  let stderrSpy: jest.SpyInstance;

  beforeEach(() => {
    redactor.clear();
    stdoutSpy = jest.spyOn(process.stdout, 'write').mockImplementation(() => true);
    stderrSpy = jest.spyOn(process.stderr, 'write').mockImplementation(() => true);
  });

  afterEach(() => {
    stdoutSpy.mockRestore();
    stderrSpy.mockRestore();
  });

  function allWrittenText(): string {
    const stdoutText = stdoutSpy.mock.calls.map((args) => String(args[0])).join('');
    const stderrText = stderrSpy.mock.calls.map((args) => String(args[0])).join('');
    return stdoutText + stderrText;
  }

  it('redacts a cookie in the primary log message', () => {
    const logger = new RedactingLogger('test-context');

    logger.log(COOKIE);

    expect(allWrittenText()).not.toContain(COOKIE);
  });

  it('redacts a cookie in an extra (non-message) argument', () => {
    const logger = new RedactingLogger('test-context');

    logger.log('some message', COOKIE);

    expect(allWrittenText()).not.toContain(COOKIE);
  });

  it('redacts both message and stack of an Error argument', () => {
    const logger = new RedactingLogger('test-context');
    const err = new Error(`request failed, cookie was ${COOKIE}`);
    err.stack = `Error: request failed, cookie was ${COOKIE}\n    at somewhere.ts:1:1`;

    logger.error(err);

    expect(allWrittenText()).not.toContain(COOKIE);
  });

  // FIX 2 (review): this test USED TO nest the cookie inside a STRING VALUE
  // -- `{ headers: { cookie: 'sessionid=sentinel-...' } }` -- so the
  // `field=value` pattern matched inside that string regardless of whether
  // the genuinely-JSON shape was ever handled at all. That made this test
  // pass even against a redactor whose field pattern required a literal `=`
  // and so could not see `redactValue`'s ACTUAL output for an object
  // argument: `JSON.stringify` renders a `sessionid` field as
  // `"sessionid":"<value>"` -- a quoted key, a COLON (not `=`), and a quoted
  // value. Naming the field `sessionid` directly, with no `=` anywhere in
  // the fixture, is what actually forces the pattern to handle
  // `"sessionid":"..."`. Confirmed RED against the pre-fix pattern before
  // widening it (see redactor.spec.ts's equivalent unit-level cases).
  it('redacts a cookie carried as a JSON field (the shape JSON.stringify actually produces for an object argument)', () => {
    const logger = new RedactingLogger('test-context');

    logger.log('context dump', { sessionid: COOKIE_VALUE_ONLY, ok: true });

    expect(allWrittenText()).not.toContain(COOKIE_VALUE_ONLY);
  });

  it('does not crash on a circular-referenced object argument', () => {
    const logger = new RedactingLogger('test-context');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const circular: any = { cookie: COOKIE };
    circular.self = circular;

    expect(() => logger.log('circular dump', circular)).not.toThrow();
    expect(allWrittenText()).not.toContain(COOKIE);
  });

  it('redacts a cookie nested inside an array argument', () => {
    const logger = new RedactingLogger('test-context');

    logger.log('array dump', ['first', COOKIE, 'last']);

    expect(allWrittenText()).not.toContain(COOKIE);
  });

  it('leaves non-secret output otherwise readable', () => {
    const logger = new RedactingLogger('test-context');

    logger.log('a perfectly normal message');

    expect(allWrittenText()).toContain('a perfectly normal message');
  });

  // Mutation guard (plan §5): `logger.log(cookie)` with the redaction call
  // removed must show the cookie. This test IS that mutation's red state
  // when someone deletes the redaction call in redact.logger.ts -- it is
  // the same assertion as the first test in this file, kept as an explicit,
  // separately-named case so the mutation table's expectation ("logger.log
  // (cookie) => red") maps onto one obviously-identifiable test.
  it('[mutation guard] logger.log(cookie) never reaches stdout/stderr unredacted', () => {
    const logger = new RedactingLogger('test-context');

    logger.log(COOKIE);

    expect(allWrittenText()).not.toContain(COOKIE);
  });
});

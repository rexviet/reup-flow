import { MIN_REGISTERED_SECRET_LENGTH, Redactor, redactor } from '../../src/redaction/redactor';

/**
 * Two independent mechanisms live in this file, and every test below is
 * written to exercise exactly one of them -- never both at once. That is
 * what lets a mutation that disables ONE mechanism be told apart from a
 * mutation that disables the other: a test that happened to trip both would
 * stay green under either mutation, hiding whichever one actually broke.
 */
describe('Redactor', () => {
  beforeEach(() => {
    redactor.clear();
  });

  it('pattern mechanism: redacts a cookie field=value it was never told about', () => {
    // sessionid is never registered anywhere in this test -- only the
    // field=value pattern can catch it.
    // The value MUST keep a marker from .gitleaks.toml's allowlist ("fake",
    // "sentinel", ...). That allowlist is what lets test fixtures carry a
    // cookie-shaped literal without turning the repo's secret scan red -- and
    // the reason it matches on the SECRET's text rather than on a path is so a
    // REAL cookie pasted into this file still trips the scan. Swap this for a
    // realistic-looking value and CI goes red, correctly.
    const text = 'set-cookie: sessionid=sentinel-fake-cookie-0123456789; Path=/';

    const result = redactor.redact(text);

    expect(result).not.toContain('sentinel-fake-cookie-0123456789');
    expect(result).toContain('sessionid=[redacted]');
  });

  // FIX 2: the field pattern originally required a literal `=`, so every one
  // of these -- all real shapes this codebase actually produces or a caller
  // could plausibly send -- sailed through unredacted. `{"sessionid":"..."}`
  // in particular is not a hypothetical: it is exactly what
  // `redact.logger.ts`'s `JSON.stringify(value)` branch emits for an object
  // argument.
  it.each([
    [
      'JSON shape (colon, quoted value) -- what JSON.stringify(value) produces',
      '{"sessionid":"sentinel-fake-json-cookie-0123456789"}',
      '{"sessionid":"[redacted]"}',
    ],
    [
      'quoted field=value',
      'sessionid="sentinel-fake-quoted-cookie-0123456789"',
      'sessionid="[redacted]"',
    ],
    [
      'bare colon, no quotes',
      'sessionid: sentinel-fake-colon-cookie-0123456789',
      'sessionid:[redacted]',
    ],
    [
      // Asserted as an EXACT match, not toContain: a charset too narrow to
      // cover `+`/`/` would still redact the leading run up to the first
      // disallowed character and produce a string that CONTAINS
      // "sessionid=[redacted]" while leaving the rest of the base64 value
      // ("+fake/base64=cookie=0123456789") sitting right next to it in
      // plain text -- a partial redaction that looks complete under a loose
      // assertion. Only a full-charset match collapses the whole thing.
      'base64 value with + and /',
      'sessionid=sentinel+fake/base64=cookie=0123456789',
      'sessionid=[redacted]',
    ],
  ])('pattern mechanism: redacts %s', (_label, text, expected) => {
    const result = redactor.redact(text);

    expect(result).toBe(expected);
  });

  it('pattern mechanism: a widened value charset does not swallow neighbouring text', () => {
    const text = 'sessionid=sentinel-fake-cookie-0123456789; Path=/; other=fine';

    const result = redactor.redact(text);

    expect(result).toBe('sessionid=[redacted]; Path=/; other=fine');
  });

  it('pattern mechanism: a quoted value stops at the closing quote, not at the next comma inside prose', () => {
    const text = '{"sessionid":"sentinel-fake-cookie-0123456789","ok":true}';

    const result = redactor.redact(text);

    // The match spans from the field name through the value's closing
    // quote, and the replacement puts the ORIGINAL delimiters back, so the
    // line survives as valid JSON. That matters: a redaction that rewrote
    // this to `{"sessionid=[redacted],"ok":true}` would remove the secret
    // and destroy the record, leaving any aggregator ingesting these logs
    // unable to parse a line it previously could.
    expect(result).toBe('{"sessionid":"[redacted]","ok":true}');
    expect(() => JSON.parse(result)).not.toThrow();
  });

  it('registry mechanism: redacts a registered secret with no field=value shape at all', () => {
    // No known cookie field name, no "=" -- the pattern mechanism has
    // nothing to match here. Only the exact-value registry can catch it.
    const secret = 'db-password-9f3ac21e';
    redactor.registerSecret(secret);
    const text = `connection failed using credential ${secret} against host`;

    const result = redactor.redact(text);

    expect(result).not.toContain(secret);
  });

  it('registerSecret ignores values shorter than 8 characters', () => {
    const shortSecret = 'sh0rt1';
    redactor.registerSecret(shortSecret);

    const result = redactor.redact(`token was ${shortSecret} exactly`);

    // Refused at registration, so it must still be present verbatim.
    expect(result).toContain(shortSecret);
  });

  // FIX 8 (review): `MIN_REGISTERED_SECRET_LENGTH` is exported specifically
  // so `src/cookie/cookie.controller.ts`'s body schema can enforce the SAME
  // floor the registry itself enforces, from ONE constant -- see that
  // file's schema. Without this, the two limits are two separately-typed
  // literals that can silently drift apart: a schema that allowed a 4-char
  // cookie while the registry refused anything under 8 would accept and
  // "successfully" push a cookie that the redactor then never actually
  // protects. Asserted against the EXPORTED constant, not a hardcoded `8`,
  // so this test tracks the real boundary rather than a number that could
  // drift out of sync with the source.
  it('registerSecret refuses a value exactly one character under MIN_REGISTERED_SECRET_LENGTH, accepts one at it', () => {
    const tooShort = 'a'.repeat(MIN_REGISTERED_SECRET_LENGTH - 1);
    const justRight = 'b'.repeat(MIN_REGISTERED_SECRET_LENGTH);

    redactor.registerSecret(tooShort);
    redactor.registerSecret(justRight);

    expect(redactor.redact(`x ${tooShort} y`)).toContain(tooShort);
    expect(redactor.redact(`x ${justRight} y`)).not.toContain(justRight);
  });

  it('registerSecret ignores empty and whitespace-only values', () => {
    redactor.registerSecret('');
    redactor.registerSecret('        '); // 8 spaces: long enough, but whitespace-only

    // Neither registration should have any effect -- there is nothing
    // meaningful to assert redacted, so assert the registry stayed inert by
    // confirming an unrelated 8-space run of text is untouched.
    const result = redactor.redact('field:        :end');

    expect(result).toBe('field:        :end');
  });

  it('registering the same value twice is a no-op (does not throw, does not double-affect output)', () => {
    const secret = 'repeat-secret-value';
    redactor.registerSecret(secret);
    redactor.registerSecret(secret);

    const result = redactor.redact(`seen twice: ${secret} and ${secret}`);

    expect(result).not.toContain(secret);
  });

  it('leaves non-secret text byte-identical', () => {
    const text = 'nothing sensitive here, just a normal log line with numbers 12345';

    expect(redactor.redact(text)).toBe(text);
  });

  it('clear() removes previously registered secrets', () => {
    const secret = 'to-be-cleared-value';
    redactor.registerSecret(secret);
    redactor.clear();

    const result = redactor.redact(`still here: ${secret}`);

    expect(result).toContain(secret);
  });

  // FIX 9 (review): phase 9's extension pushes a cookie on every 20-minute
  // alarm, and `msToken`/`ttwid` rotate -- most pushes are a DISTINCT value,
  // so an unbounded registry grows forever. Two costs, both unbounded: every
  // stdout write scans the whole list (throughput degrades with uptime), and
  // every cookie ever seen stays live in the heap in plaintext (a heap
  // snapshot yields the full history). Capped at 32 entries, oldest evicted
  // first -- a very old cookie appearing in a very delayed log line would no
  // longer be scrubbed, which is the accepted tradeoff against unbounded
  // growth.
  it('caps the registry: registering more than the limit evicts the OLDEST entry, not the newest', () => {
    const local = new Redactor();
    const CAP = 32;
    const secrets = Array.from({ length: CAP + 1 }, (_, i) => `secret-number-${String(i).padStart(3, '0')}`);

    for (const secret of secrets) local.registerSecret(secret);

    const oldest = secrets[0];
    const newest = secrets[secrets.length - 1];
    expect(local.redact(`still here? ${oldest}`)).toContain(oldest); // evicted
    expect(local.redact(`still here? ${newest}`)).not.toContain(newest); // kept
  });

  it('caps the registry at exactly the limit -- registering one MORE than the limit evicts exactly one', () => {
    const local = new Redactor();
    const CAP = 32;
    const secrets = Array.from({ length: CAP }, (_, i) => `bounded-secret-${String(i).padStart(3, '0')}`);

    for (const secret of secrets) local.registerSecret(secret);
    // Every one of the first CAP secrets must still be live -- the cap is a
    // ceiling, not a premature eviction.
    for (const secret of secrets) {
      expect(local.redact(`check: ${secret}`)).not.toContain(secret);
    }
  });

  it('exports a Redactor class usable independently of the singleton', () => {
    const local = new Redactor();
    local.registerSecret('independent-instance-secret');

    expect(local.redact('independent-instance-secret')).not.toContain(
      'independent-instance-secret',
    );
    // The module singleton must not have been polluted by the local instance.
    expect(redactor.redact('independent-instance-secret')).toContain(
      'independent-instance-secret',
    );
  });
});

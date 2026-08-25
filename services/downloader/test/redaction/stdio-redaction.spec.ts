import { installStdioRedaction } from '../../src/redaction/stdio-redaction';
import { redactor } from '../../src/redaction/redactor';

/**
 * This is the phase-1 lesson made structural: the historical leak in the
 * parser fork was a bare `print()`, and a test that only watched the
 * logging subsystem let it through clean. These tests therefore write
 * directly to `console.log` and `process.stderr.write` -- never through any
 * logger abstraction -- because that funnel is exactly what leaked before.
 */
describe('installStdioRedaction', () => {
  // The value MUST keep a marker from .gitleaks.toml's allowlist ("fake",
  // "sentinel", ...). That allowlist is what lets test fixtures carry a
  // cookie-shaped literal without turning the repo's secret scan red -- and
  // the reason it matches on the SECRET's text rather than on a path is so a
  // REAL cookie pasted into this file still trips the scan. Swap this for a
  // realistic-looking value and CI goes red, correctly.
  const COOKIE = 'sessionid=sentinel-fake-cookie-0123456789';
  let uninstall: () => void;
  let stdoutChunks: string[];
  let stderrChunks: string[];
  let realStdoutWrite: typeof process.stdout.write;
  let realStderrWrite: typeof process.stderr.write;

  beforeEach(() => {
    redactor.clear();
    stdoutChunks = [];
    stderrChunks = [];
    // Capture at the lowest level BEFORE installing the patch, so these
    // arrays record whatever ultimately reaches the real streams -- whether
    // or not the patch is installed. Doing the capture by wrapping
    // process.stdout.write ourselves and then letting installStdioRedaction
    // wrap OUR wrapper would prove nothing about the real funnel.
    realStdoutWrite = process.stdout.write.bind(process.stdout);
    realStderrWrite = process.stderr.write.bind(process.stderr);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (process.stdout as any).write = (chunk: any, ...rest: any[]) => {
      stdoutChunks.push(decodeChunk(chunk));
      return realStdoutWrite(chunk, ...rest);
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (process.stderr as any).write = (chunk: any, ...rest: any[]) => {
      stderrChunks.push(decodeChunk(chunk));
      return realStderrWrite(chunk, ...rest);
    };
  });

  /**
   * `chunk.toString()` is only a text decode for a `string` or a `Buffer`.
   * A plain `Uint8Array` (not a `Buffer` instance -- e.g. a
   * `TextEncoder().encode(...)` result) inherits `Array.prototype.toString`,
   * which renders it as comma-separated decimal byte values instead of text.
   * A capture harness that used the bare `.toString()` here would silently
   * hide whatever text a Uint8Array chunk carried -- a cookie inside one
   * would never show up as a substring of the captured text either way,
   * making a real leak indistinguishable from a correct redaction. Decoding
   * through `Buffer.from` first is what makes this harness capable of
   * observing a Uint8Array chunk's actual text at all.
   */
  function decodeChunk(chunk: unknown): string {
    if (typeof chunk === 'string') return chunk;
    if (chunk instanceof Uint8Array) return Buffer.from(chunk).toString('utf8');
    return String(chunk);
  }

  afterEach(() => {
    if (uninstall) uninstall();
    process.stdout.write = realStdoutWrite;
    process.stderr.write = realStderrWrite;
  });

  it('redacts console.log output while installed', () => {
    uninstall = installStdioRedaction();

    console.log(COOKIE);

    expect(stdoutChunks.join('')).not.toContain(COOKIE);
  });

  it('redacts process.stderr.write output while installed', () => {
    uninstall = installStdioRedaction();

    process.stderr.write(`${COOKIE}\n`);

    expect(stderrChunks.join('')).not.toContain(COOKIE);
  });

  it('positive control: after uninstall(), the secret flows through again', () => {
    // Without this half, the two tests above cannot be told apart from "the
    // capture harness itself is broken" -- a harness that silently dropped
    // every chunk would also show no cookie, for the wrong reason.
    uninstall = installStdioRedaction();
    uninstall();

    process.stderr.write(`${COOKIE}\n`);

    expect(stderrChunks.join('')).toContain(COOKIE);
  });

  it('is idempotent: installing twice does not double-wrap', () => {
    const uninstallFirst = installStdioRedaction();
    const uninstallSecond = installStdioRedaction();

    // A single uninstall() must be enough to fully restore the original
    // write -- if install had double-wrapped, one uninstall would still
    // leave a redacting layer active.
    uninstallSecond();

    process.stderr.write(`${COOKIE}\n`);
    expect(stderrChunks.join('')).not.toContain(COOKIE);

    uninstallFirst();
    process.stderr.write(`${COOKIE}\n`);
    expect(stderrChunks.join('')).toContain(COOKIE);

    uninstall = () => {
      /* already fully uninstalled above */
    };
  });

  it('passes an unchanged Buffer through untouched when it contains no secret', () => {
    uninstall = installStdioRedaction();
    // Trailing `\n` -- FIX 6's line-buffering (below) holds a chunk with no
    // newline in `pending` until more data completes the line, an overflow,
    // an `uninstall()`, or process exit flush it. A complete line flushes
    // immediately, which is what this test needs to observe synchronously.
    const buf = Buffer.from('plain binary-ish payload, no secrets here\n', 'utf8');

    process.stderr.write(buf);

    const written = stderrChunks.join('');
    expect(written).toContain('plain binary-ish payload, no secrets here');
  });

  it('redacts a secret carried in a Buffer chunk', () => {
    uninstall = installStdioRedaction();
    const buf = Buffer.from(`${COOKIE}\n`, 'utf8');

    process.stderr.write(buf);

    expect(stderrChunks.join('')).not.toContain(COOKIE);
  });

  // FIX 1: `Buffer.isBuffer()` is false for a plain `Uint8Array` -- Buffer is
  // a subclass, but not every ArrayBufferView is one. A library that writes
  // via `TextEncoder().encode(...)` (a plain Uint8Array, not a Buffer) used
  // to fall through the Buffer branch AND the string branch and hit the
  // catch-all "pass through unchanged" path, forwarding a secret raw. This is
  // the reproduction from the review: encode a cookie with TextEncoder, write
  // it, and require it to come out redacted like any other chunk shape.
  it('redacts a secret carried in a plain Uint8Array chunk (not a Buffer instance)', () => {
    uninstall = installStdioRedaction();
    const encoded = new TextEncoder().encode(`${COOKIE}\n`);
    expect(Buffer.isBuffer(encoded)).toBe(false); // guards the premise of this test

    process.stderr.write(encoded as unknown as string);

    expect(stderrChunks.join('')).not.toContain(COOKIE);
  });

  it('passes an unchanged plain Uint8Array chunk through byte-for-byte when it contains no secret', () => {
    uninstall = installStdioRedaction();
    // Trailing `\n` -- see the comment on the Buffer equivalent above.
    const text = 'plain binary-ish payload, no secrets here\n';
    const encoded = new TextEncoder().encode(text);

    process.stderr.write(encoded as unknown as string);

    expect(stderrChunks.join('')).toContain(text.trimEnd());
  });

  // FIX 6: without a per-stream carry-over, each write() call is redacted in
  // isolation. A secret split across two calls -- the first ending mid-value,
  // the second carrying the tail -- never appears whole in EITHER chunk, so
  // neither redaction pass ever sees the full pattern to match. Worse: the
  // FIRST chunk alone can still satisfy the pattern's minimum-length
  // requirement on its own truncated prefix, producing a match that LOOKS
  // like a complete, correct redaction (`sessionid=[redacted]`) while the
  // second write then emits the untouched tail right after it -- output that
  // reads as "this was handled" while still leaking the rest of the value.
  it('redacts a secret split across two write() calls, never emitting it whole OR partially', () => {
    uninstall = installStdioRedaction();
    // Split mid-value, deliberately AFTER the point where the pattern's
    // 8-char minimum is already satisfied -- e.g. a naive per-chunk redactor
    // would happily match+redact `sessionid=sentinel-` on its own (well over
    // the minimum) and emit what looks like a complete `sessionid=[redacted]`
    // BEFORE the second write ever arrives, then emit the tail
    // (`fake-cookie-0123456789`) unredacted right after it.
    const splitPoint = COOKIE.indexOf('sentinel-') + 'sentinel-'.length;
    const firstHalf = COOKIE.slice(0, splitPoint); // 'sessionid=sentinel-'
    const secondHalf = COOKIE.slice(splitPoint); // 'fake-cookie-0123456789'
    expect(firstHalf + secondHalf).toBe(COOKIE);

    process.stderr.write(firstHalf);
    process.stderr.write(`${secondHalf}\n`);

    const written = stderrChunks.join('');
    expect(written).not.toContain(COOKIE);
    expect(written).not.toContain(secondHalf);
    expect(written).toContain('sessionid=[redacted]');
  });

  it('holds a chunk with no newline in `pending` rather than emitting it immediately', () => {
    uninstall = installStdioRedaction();

    process.stderr.write('no newline yet, still forming a line');

    // Nothing reaches the real stream until the line completes -- this is
    // what makes the two-call split test above possible at all.
    expect(stderrChunks.join('')).toBe('');
  });

  it('flushes a held, unterminated line on uninstall() rather than swallowing it', () => {
    uninstall = installStdioRedaction();

    process.stderr.write('unterminated line held in pending');
    expect(stderrChunks.join('')).toBe('');

    uninstall();
    uninstall = () => {
      /* already uninstalled above */
    };

    expect(stderrChunks.join('')).toContain('unterminated line held in pending');
  });

  it('flushes a held, unterminated line on process exit rather than swallowing it', () => {
    uninstall = installStdioRedaction();

    process.stderr.write(`${COOKIE}, still forming a line with no newline`);
    expect(stderrChunks.join('')).toBe('');

    process.emit('exit', 0);

    const written = stderrChunks.join('');
    expect(written).toContain('still forming a line with no newline');
    expect(written).not.toContain(COOKIE);
  });

  it('caps unbounded pending growth: an overflowing line-less write is flushed rather than accumulated forever', () => {
    uninstall = installStdioRedaction();
    // Comfortably over the 64 KiB cap with no newline anywhere in it.
    const huge = 'x'.repeat(70 * 1024);

    process.stderr.write(huge);

    // The overflow flush must have happened SYNCHRONOUSLY, inside this one
    // write() call -- not held forever waiting for a newline that never
    // comes, which is exactly the unbounded-memory failure mode this cap
    // exists to prevent.
    expect(stderrChunks.join('').length).toBeGreaterThan(0);
  });

  it('preserves the write return value and invokes the callback', () => {
    // Jest's own stdout/stderr capture in this environment does not reliably
    // invoke write() callbacks at all (verified independently: a bare,
    // unpatched `process.stderr.write('x', cb)` with no redaction code in
    // the picture also never calls `cb` here), so asserting against the
    // real stream would test Jest's plumbing, not this module. Instead,
    // substitute a fully-controlled fake as the "original" write that
    // installStdioRedaction captures, and assert the patched write forwards
    // to it with the exact return value and invokes the given callback.
    const calls: unknown[][] = [];
    const fakeOriginal = (...args: unknown[]): boolean => {
      calls.push(args);
      const cb = args.find((a): a is () => void => typeof a === 'function');
      cb?.();
      return false; // deliberately distinct from the default `true`, so a
      // dropped return value cannot go unnoticed by coincidence.
    };
    process.stderr.write = fakeOriginal as typeof process.stderr.write;

    uninstall = installStdioRedaction();

    let callbackInvoked = false;
    const returnValue = process.stderr.write('no secret here\n', () => {
      callbackInvoked = true;
    });

    expect(callbackInvoked).toBe(true);
    expect(returnValue).toBe(false);
    expect(calls).toHaveLength(1);
  });
});

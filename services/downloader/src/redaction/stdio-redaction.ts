import { redactor } from './redactor';

/**
 * Patches `process.stdout.write` and `process.stderr.write` so EVERYTHING
 * leaving the process through those two funnels is redacted --
 * `console.log`, a bare `process.stderr.write`, a third-party library's own
 * debug output, Nest's own logger underneath. This is the phase-1 lesson
 * made structural: the historical leak in the parser fork was a `print()`,
 * and a test that only watched the logging subsystem let it through clean.
 * `src/logging/redact.logger.ts` also redacts before delegating to Nest's
 * ConsoleLogger, which itself writes through this same patched funnel --
 * that overlap is deliberate depth, not redundancy to trim.
 */

type WriteFn = typeof process.stdout.write;

interface Installation {
  restoreStdout: WriteFn;
  restoreStderr: WriteFn;
  flushStdout: () => void;
  flushStderr: () => void;
  exitHandler: () => void;
}

/** Tracks the one active installation so a second install() cannot double-wrap. */
let installed: Installation | null = null;

/**
 * `write` has two call shapes: `write(chunk, callback?)` and
 * `write(chunk, encoding?, callback?)`. Node picks whichever argument is a
 * function as the callback; this mirrors that so a caller using either shape
 * still gets its callback invoked.
 */
function splitArgs(
  encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
  callback?: (error?: Error | null) => void,
): { encoding: BufferEncoding | undefined; cb: ((error?: Error | null) => void) | undefined } {
  if (typeof encodingOrCallback === 'function') {
    return { encoding: undefined, cb: encodingOrCallback };
  }
  return { encoding: encodingOrCallback, cb: callback };
}

/**
 * Calls `original` with exactly the arguments the caller originally
 * supplied (chunk [, encoding] [, callback]) rather than a padded
 * `(chunk, undefined, callback)` shape. Some environments (Jest's own
 * stdout/stderr interception among them) install a `write` whose arity
 * checks are position-sensitive, so synthesising an extra `undefined`
 * encoding argument can silently swallow the callback. Preserving the
 * caller's original arity keeps this patch transparent to whatever it is
 * layered on top of.
 */
// Cast to a single loosely-typed signature for the actual invocation below:
// `write`'s real type is a TS overload union that `.call()` cannot resolve
// generically across the four call shapes this helper dispatches to.
type LooseWriteFn = (...args: unknown[]) => boolean;

function callOriginal(
  original: WriteFn,
  stream: NodeJS.WriteStream,
  chunk: string | Buffer | Uint8Array,
  encoding: BufferEncoding | undefined,
  cb: ((error?: Error | null) => void) | undefined,
): boolean {
  const fn = original as unknown as LooseWriteFn;
  if (encoding !== undefined && cb !== undefined) {
    return fn.call(stream, chunk, encoding, cb);
  }
  if (encoding !== undefined) {
    return fn.call(stream, chunk, encoding);
  }
  if (cb !== undefined) {
    return fn.call(stream, chunk, cb);
  }
  return fn.call(stream, chunk);
}

/**
 * Decodes any chunk `write()` can be handed into a plain string, the shape
 * everything below this point operates on.
 *
 * `Buffer.isBuffer()` is false for a plain `Uint8Array` -- Buffer is a
 * subclass of it, not the other way round, so e.g. a
 * `TextEncoder().encode(...)` result (used by some third-party libraries
 * that write to stdout/stderr directly) would otherwise fall through both
 * the Buffer branch and the string branch into a catch-all "pass through
 * unchanged" path, forwarding a secret completely unscrubbed.
 * `ArrayBuffer.isView` is true for every typed-array/DataView shape
 * `write()` can be handed, Buffer included, so wrapping any of them in a
 * Buffer view (no copy -- same underlying bytes) puts them all through one
 * decode path.
 */
function decode(chunk: string | Buffer | Uint8Array, encoding: BufferEncoding | undefined): string {
  if (typeof chunk === 'string') return chunk;
  const view = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);
  // Default utf8, per the task spec -- everything this process itself writes
  // (console.log, our own error text) is utf8; a stream that declares
  // another encoding still gets decoded with it.
  return view.toString(encoding ?? 'utf8');
}

/**
 * Upper bound on how much unterminated text one stream's `pending` carry-over
 * may hold before it is force-flushed. Without a cap, a library that writes a
 * very long line with no trailing newline (or never writes one at all) would
 * make `pending` grow without bound -- an unbounded-memory leak traded for
 * catching a boundary case that, in practice, never spans anywhere near 64
 * KiB (real cookie values and DSNs are at most a few hundred bytes). Chosen
 * generously above any realistic single log line while still being a hard
 * ceiling: on overflow the accumulated text is redacted and flushed
 * immediately, accepting the (rare, bounded) risk that a secret straddling
 * exactly this cut point is only partially redacted, rather than growing
 * `pending` forever.
 */
const MAX_PENDING_BYTES = 64 * 1024;

/**
 * Per-stream line-buffering state (plan §0.3 / FIX 6): redacting one
 * `write()` call in isolation misses a secret split across two calls, and
 * -- worse -- can emit a PARTIAL redaction that looks complete (the
 * pattern's minimum-length match satisfied entirely within the first call)
 * while the second call then emits the unredacted tail right after it.
 *
 * The fix relies on the one fact that makes it safe to buffer by LINE rather
 * than by call: no secret this redactor handles (a cookie value, a DSN) ever
 * contains a newline. So everything up to and including the last `\n` in
 * `pending + newText` is guaranteed to contain any secret that started
 * inside it wholly, and can be redacted and flushed immediately -- there is
 * no output delay at all for ordinary line-oriented logging, which is
 * virtually everything this process writes. Only a genuinely unterminated
 * partial line is held back, and only until the next write completes it, an
 * `uninstall()`, or process exit flush it.
 */
class LineBuffer {
  private pending = '';

  constructor(
    private readonly original: WriteFn,
    private readonly stream: NodeJS.WriteStream,
  ) {}

  /**
   * Appends `text`, flushing everything up to (and including) the last
   * newline immediately. Returns what to hand back to the caller of
   * `write()`: `{ shouldForward: true, text }` when there is something to
   * write to the real stream right now, `{ shouldForward: false }` when the
   * whole chunk was absorbed into `pending` with nothing left to flush yet.
   */
  append(text: string): { shouldForward: boolean; text: string } {
    const combined = this.pending + text;
    const lastNewline = combined.lastIndexOf('\n');

    if (lastNewline === -1) {
      this.pending = combined;
      if (Buffer.byteLength(this.pending, 'utf8') > MAX_PENDING_BYTES) {
        // Overflow: flush the whole accumulated head now rather than let it
        // grow forever. See MAX_PENDING_BYTES's comment for the accepted
        // tradeoff.
        const overflowed = this.pending;
        this.pending = '';
        return { shouldForward: true, text: redactor.redact(overflowed) };
      }
      // Nothing to flush yet -- still forming a line.
      return { shouldForward: false, text: '' };
    }

    const toFlush = combined.slice(0, lastNewline + 1);
    this.pending = combined.slice(lastNewline + 1);
    return { shouldForward: true, text: redactor.redact(toFlush) };
  }

  /**
   * Flushes whatever unterminated line is currently held, redacted like
   * everything else. Called on `uninstall()` and on process `exit` so a
   * final line with no trailing newline is not silently swallowed.
   * Idempotent: flushing with nothing pending is a no-op.
   */
  flush(): void {
    if (this.pending.length === 0) return;
    const redacted = redactor.redact(this.pending);
    this.pending = '';
    callOriginal(this.original, this.stream, redacted, undefined, undefined);
  }
}

function wrap(stream: NodeJS.WriteStream, original: WriteFn): { write: WriteFn; flush: () => void } {
  const buffer = new LineBuffer(original, stream);

  const patched = function patchedWrite(
    this: NodeJS.WriteStream,
    chunk: string | Buffer | Uint8Array,
    encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
    callback?: (error?: Error | null) => void,
  ): boolean {
    const { encoding, cb } = splitArgs(encodingOrCallback, callback);
    const text = decode(chunk, encoding);
    const result = buffer.append(text);

    if (!result.shouldForward) {
      // Nothing handed to the real stream yet -- write()'s contract still
      // guarantees the callback fires, and there is no backpressure to
      // report since nothing was actually queued downstream.
      cb?.();
      return true;
    }

    return callOriginal(original, stream, result.text, undefined, cb);
  };

  return { write: patched as WriteFn, flush: () => buffer.flush() };
}

/**
 * Installs the redacting patch. Idempotent: a second call while already
 * installed does NOT wrap the (already wrapped) write function again --
 * doing so would mean a single `uninstall()` peels off only the outermost
 * layer and leaves redaction silently still active, which is worse than
 * either fully on or fully off because it looks uninstalled while it isn't.
 * Instead, a redundant call returns a no-op handle; only the handle from the
 * call that actually installed the patch can undo it.
 */
export function installStdioRedaction(): () => void {
  if (installed) {
    return () => {
      /* This call did not own the installation; nothing to undo here. */
    };
  }

  const restoreStdout = process.stdout.write.bind(process.stdout) as WriteFn;
  const restoreStderr = process.stderr.write.bind(process.stderr) as WriteFn;

  const stdout = wrap(process.stdout, restoreStdout);
  const stderr = wrap(process.stderr, restoreStderr);

  // A final unterminated line (no trailing newline) sitting in `pending`
  // when the process exits would otherwise be silently dropped -- there is
  // no later write() call to complete the line and trigger its flush.
  const exitHandler = (): void => {
    stdout.flush();
    stderr.flush();
  };
  process.on('exit', exitHandler);

  installed = {
    restoreStdout,
    restoreStderr,
    flushStdout: stdout.flush,
    flushStderr: stderr.flush,
    exitHandler,
  };

  process.stdout.write = stdout.write;
  process.stderr.write = stderr.write;

  return function uninstall(): void {
    if (!installed) return;
    // Flush BEFORE restoring the original write functions: once restored,
    // this instance's `pending` state becomes unreachable, and a held
    // unterminated line would be lost for good.
    installed.flushStdout();
    installed.flushStderr();
    process.off('exit', installed.exitHandler);
    process.stdout.write = installed.restoreStdout;
    process.stderr.write = installed.restoreStderr;
    installed = null;
  };
}

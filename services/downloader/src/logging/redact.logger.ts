import { ConsoleLogger, Injectable, type LoggerService } from '@nestjs/common';
import { redactor } from '../redaction/redactor';

/**
 * Recursively redacts a value bound for a log call. Nest's LoggerService
 * methods accept `any`, so a call site can (and, per plan §0.3, eventually
 * will) hand this a string, an Error, a plain object, or an array of any of
 * those -- every shape here has to come out with no secret substring
 * surviving.
 *
 * `Error` gets special handling because its two carriers of leaked text
 * (`message`, `stack`) are non-enumerable and would otherwise fall straight
 * through the generic "serialise as JSON" branch below with `JSON.stringify
 * (new Error(...))` producing `{}` -- redacting nothing while looking like
 * it redacted the whole object.
 *
 * Plain objects and arrays are NOT walked field-by-field and reconstructed;
 * they are serialised once via `JSON.stringify` and the resulting string is
 * redacted. Reconstructing a redacted deep clone would need to handle every
 * JS value shape (Map, Set, Date, getters, non-enumerable props, ...) to
 * stay accurate, whereas string redaction only needs the fully-rendered
 * text to be correct once. `JSON.stringify` throws on a circular
 * structure -- caught below so a call site with a genuinely circular object
 * degrades to a placeholder instead of crashing the process it was trying
 * to log from.
 */
function redactValue(value: unknown): unknown {
  if (typeof value === 'string') {
    return redactor.redact(value);
  }

  if (value instanceof Error) {
    // A shallow clone: the caller's Error object is never mutated in place,
    // since callers sometimes log-then-rethrow the same instance and must
    // not see it silently altered underneath them.
    const redacted = new Error(redactor.redact(value.message));
    redacted.name = value.name;
    redacted.stack = value.stack ? redactor.redact(value.stack) : value.stack;
    return redacted;
  }

  if (Array.isArray(value)) {
    return value.map(redactValue);
  }

  if (value !== null && typeof value === 'object') {
    let serialised: string;
    try {
      serialised = JSON.stringify(value);
    } catch {
      // Circular reference, a BigInt, or anything else JSON.stringify
      // refuses. Whatever it was, it cannot be inspected for secrets, so
      // fail toward NOT printing it rather than crashing the logger.
      return '[unserialisable value, omitted by RedactingLogger]';
    }
    return redactor.redact(serialised);
  }

  // number, boolean, undefined, null, bigint, symbol, function -- none of
  // these can carry a string secret.
  return value;
}

/**
 * Wraps Nest's ConsoleLogger, redacting the message and every extra
 * argument before delegating. This is a SECOND, overlapping mechanism on
 * top of `src/redaction/stdio-redaction.ts` -- that patch would also catch
 * whatever ConsoleLogger ultimately writes to stdout/stderr underneath this
 * class. The overlap is deliberate depth (plan §0.3: two independent
 * layers, not one relied on twice), which is exactly why
 * test/logging/redact.logger.spec.ts runs with the stdio patch NOT
 * installed -- only that way does a passing test prove THIS class redacts,
 * rather than proving the stdio patch does.
 */
@Injectable()
export class RedactingLogger implements LoggerService {
  private readonly inner: ConsoleLogger;

  constructor(context?: string) {
    this.inner = new ConsoleLogger(context ?? RedactingLogger.name);
  }

  log(message: unknown, ...optionalParams: unknown[]): void {
    this.inner.log(redactValue(message), ...(optionalParams.map(redactValue) as [string?]));
  }

  error(message: unknown, ...optionalParams: unknown[]): void {
    this.inner.error(
      redactValue(message),
      ...(optionalParams.map(redactValue) as [string?, string?]),
    );
  }

  warn(message: unknown, ...optionalParams: unknown[]): void {
    this.inner.warn(redactValue(message), ...(optionalParams.map(redactValue) as [string?]));
  }

  debug(message: unknown, ...optionalParams: unknown[]): void {
    this.inner.debug(redactValue(message), ...(optionalParams.map(redactValue) as [string?]));
  }

  verbose(message: unknown, ...optionalParams: unknown[]): void {
    this.inner.verbose(redactValue(message), ...(optionalParams.map(redactValue) as [string?]));
  }

  fatal(message: unknown, ...optionalParams: unknown[]): void {
    this.inner.fatal(redactValue(message), ...(optionalParams.map(redactValue) as [string?]));
  }
}

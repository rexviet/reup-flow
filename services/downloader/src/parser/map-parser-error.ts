import { BadGatewayException, Logger, ServiceUnavailableException } from '@nestjs/common';
import { ParserRejectedError, ParserUnavailableError } from './parser.errors';

/** Logs the internal detail `mapParserError` deliberately keeps out of a
 * ParserUnavailableError's HTTP response -- see that function's comment. */
const parserErrorLogger = new Logger('ParserErrors');

/**
 * F4 -- the parser fork collapses every internal exception into an
 * information-free HTTP 400 (see parser.errors.ts). This is the one place
 * that translates the two error classes the client can actually throw into
 * HTTP statuses, and it does so WITHOUT inventing a cause for either:
 *
 *   - ParserUnavailableError -- no verdict was reached at all (connection
 *     refused, timeout, 5xx, bad envelope). 503: retry later may help.
 *   - ParserRejectedError -- the parser answered with a refusal. 502: this
 *     service's request to its upstream failed: it does NOT restate F4's
 *     "cause unknown" 400 as a diagnosis of what went wrong.
 *
 * A parser error must never escape this handler as an unmapped 500 -- that
 * would look like a bug in this service when the actual failure is
 * upstream.
 *
 * `ParserUnavailableError`'s message can embed `ParserClient`'s
 * `baseUrl` (the "did not answer GET ... at http://parser:80 ..." variant,
 * see parser.client.ts's `get`) -- internal compose-network topology that an
 * API client has no business learning about this service's deployment.
 * `ParserRejectedError`'s messages never carry that detail (they describe a
 * route/status, not an address), so only the 503 path is sanitised here; the
 * full detail is still logged (via Nest's `Logger`, whose output still goes
 * through the process-wide stdio redaction patch like everything else this
 * process writes, see redaction/stdio-redaction.ts) so an operator debugging
 * connectivity has not lost it, it is just no longer in the response body.
 */
export function mapParserError(err: unknown): Error {
  if (err instanceof ParserUnavailableError) {
    parserErrorLogger.error(err.message);
    return new ServiceUnavailableException('parser is unavailable');
  }
  if (err instanceof ParserRejectedError) return new BadGatewayException(err.message);
  return err instanceof Error ? err : new Error(String(err));
}

import type { Readable } from 'node:stream';
import { Agent, request, type Dispatcher } from 'undici';

/**
 * The Referer `*.douyinvod.com` will not serve a video without.
 *
 * Not a nicety and not anti-bot theatre from our side: the CDN checks it, and
 * without it the same signed URL that works in a browser answers 403 -- which
 * looks exactly like an expired signature and would send the lane into a
 * pointless re-resolve loop, spending the request budget F1 exists to protect.
 */
export const DOUYIN_REFERER = 'https://www.douyin.com/';

/**
 * The User-Agent the parser fork signs its Douyin-web requests with.
 *
 * "UA khớp cookie" (phase-06 §6.2 step 4). The cookie in Redis was minted for a
 * browser, the fork's douyin/web crawler sends this exact UA alongside it (its
 * own config.yaml carries a comment telling you not to change it), and the
 * `play_addr` we are about to GET was issued to that pairing. Downloading with a
 * different UA is a mismatch the CDN is entitled to refuse -- and the refusal
 * arrives as a 403, indistinguishable from an expired URL.
 *
 * `test/cdn/cdn.client.spec.ts` reads the fork's config.yaml and asserts this
 * string still equals it, so an upstream bump fails a test here rather than
 * turning every download into a 403 in production. Plan §0 principle 2 forbids
 * patching the fork, so copying the value with a pinned test is the option that
 * is left.
 */
export const DOUYIN_WEB_USER_AGENT =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) ' +
  'Chrome/90.0.4430.212 Safari/537.36';

/**
 * Generous, and for a different reason than the parser's budget.
 *
 * This is the whole-call ceiling for HEADERS, not for the body: a 200MB video
 * on a slow link legitimately takes minutes, so a body timeout measured over
 * the whole transfer would cancel healthy downloads. `bodyTimeout` in undici is
 * an INACTIVITY timeout, which is the right shape -- it fires on a stalled
 * socket and never on a slow one.
 */
const DEFAULT_TIMEOUT_MS = 30_000;

export interface CdnClientOptions {
  /** Headers + connect budget, and the inactivity ceiling for the body. */
  timeoutMs?: number;
  /** Tests only -- production must let the client own its dispatcher. */
  dispatcher?: Dispatcher;
}

export interface CdnResponse {
  /** Whatever the CDN said. 403/410 are answers, not errors -- see `get`. */
  status: number;
  /**
   * The advertised body size, or undefined when the CDN sent no usable
   * `Content-Length`.
   *
   * NEVER a fabricated number. `Storage.put` treats this as the truncation
   * yardstick, so a synthetic 0 would reject every download and a guessed
   * length would be a check against something nobody sent.
   */
  contentLength?: number;
  body: Readable;
  /** Releases the connection without reading the body. */
  discard(): Promise<void>;
}

/**
 * The only way the download lane touches the internet.
 *
 * Deliberately thinner than `ParserClient`: there is no envelope, no cookie and
 * no application-level verdict to unwrap. What it does own is the two headers
 * above, the no-retry rule, and handing a STATUS back rather than an exception
 * for the one status that means something specific to the caller.
 */
export interface CdnClient {
  get(url: string, signal: AbortSignal): Promise<CdnResponse>;
  /** Releases sockets so a shutdown can actually exit. */
  close(): Promise<void>;
}

/** Thrown when nothing answered, or the address could not be used at all. */
export class CdnUnavailableError extends Error {
  constructor(
    message: string,
    readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'CdnUnavailableError';
  }
}

export class UndiciCdnClient implements CdnClient {
  private readonly timeoutMs: number;
  /**
   * Its OWN dispatcher, never the process-wide one, for the reason
   * `ParserClient` spells out at length: `setGlobalDispatcher(new
   * RetryAgent(...))` anywhere in this process would turn one GET into four,
   * and no mutation of this file could catch a change made in another one.
   *
   * It also gives the client something to `close()`, which phase 4's graceful
   * shutdown needs -- a keep-alive socket on the event loop is a process that
   * does not exit.
   */
  private readonly dispatcher: Dispatcher;

  constructor(options: CdnClientOptions = {}) {
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.dispatcher =
      options.dispatcher ??
      new Agent({
        connect: { timeout: this.timeoutMs },
        headersTimeout: this.timeoutMs,
        // Inactivity, not total transfer time. See DEFAULT_TIMEOUT_MS.
        bodyTimeout: this.timeoutMs,
      });
  }

  async close(): Promise<void> {
    await this.dispatcher.close();
  }

  /**
   * One GET, one request. No retries, ever -- retry policy belongs to the lane
   * (phase-06 §6.3), which is the only layer that knows how many attempts this
   * video has left.
   *
   * A non-2xx answer is RETURNED, not thrown. 403 and 410 are the whole of F1's
   * reactive half: they mean "that signature is stale, go and get another one",
   * which is a different instruction from "the network is down" -- and a lane
   * that had to tell them apart by parsing an error message would get it wrong
   * the first time a CDN changed its error page.
   *
   * `new URL` is inside the try because `play_addr` comes off Douyin: a
   * malformed one throws a raw `TypeError [ERR_INVALID_URL]`, which is not a
   * class any lane handler matches, so it would escape as an unrecognised
   * exception. The same trap `ParserClient.get` documents.
   */
  async get(url: string, signal: AbortSignal): Promise<CdnResponse> {
    try {
      const target = new URL(url);
      const res = await request(target, {
        method: 'GET',
        dispatcher: this.dispatcher,
        signal,
        headersTimeout: this.timeoutMs,
        bodyTimeout: this.timeoutMs,
        headers: {
          referer: DOUYIN_REFERER,
          'user-agent': DOUYIN_WEB_USER_AGENT,
          // Explicit: a proxy that decided to gzip an mp4 would make the
          // advertised length disagree with the bytes written, and the
          // truncation check would report a corruption that is not one.
          'accept-encoding': 'identity',
        },
      });

      return {
        status: res.statusCode,
        contentLength: parseContentLength(res.headers['content-length']),
        body: res.body,
        discard: () => res.body.dump().then(() => undefined),
      };
    } catch (cause) {
      throw new CdnUnavailableError(
        `cdn did not answer GET within ${this.timeoutMs}ms, or the address could not be used ` +
          `at all (the address itself is deliberately not repeated here: play_addr is a signed ` +
          `URL and its query carries credentials)`,
        cause,
      );
    }
  }
}

/**
 * Reads `Content-Length`, or answers undefined.
 *
 * Undefined for a missing header, a non-numeric one, an array (undici hands
 * back an array when a header repeats), or a negative number. Every one of
 * those would otherwise reach `Storage.put` as `NaN` -- and `bytes !== NaN` is
 * always true, so the truncation check would reject every download it was meant
 * to protect.
 */
function parseContentLength(raw: string | string[] | undefined): number | undefined {
  if (typeof raw !== 'string') return undefined;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 0) return undefined;
  return value;
}

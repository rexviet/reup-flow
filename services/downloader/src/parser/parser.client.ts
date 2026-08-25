import { Agent, request, type Dispatcher } from 'undici';
import { ParserRejectedError, ParserUnavailableError } from './parser.errors';

export interface ParserClientOptions {
  baseUrl: string;
  /** Whole-call budget: connect + headers + body. */
  timeoutMs?: number;
  /**
   * Override the client's private dispatcher. Tests only -- production must
   * let the client own one, see the field comment below.
   */
  dispatcher?: Dispatcher;
}

/** One item of `aweme_list`, exactly as Douyin sent it. Never reshaped. */
export type Aweme = Record<string, unknown>;

export interface UserPostPage {
  awemeList: Aweme[];
  hasMore: boolean;
  maxCursor: number;
  statusCode: DouyinStatusCode;
}

/**
 * Douyin's own application-level verdict, carried through unchanged.
 *
 * `0` is Douyin's success value; anything else is a refusal. The fork hands
 * back any Douyin 200 body verbatim and stamps its own `code: 200` envelope
 * on it, so a refusal at this layer looks like a completely healthy HTTP
 * exchange. Phase 7's F4 probe reads a successful `fetchUserProfile` as "the
 * cookie is still good" -- if this field were dropped, that probe would
 * report a refused cookie as healthy and send an operator to investigate the
 * profile instead.
 *
 * `null` means Douyin sent no `status_code` at all. Deliberately not
 * defaulted to `0`: inventing success that was never observed is the same
 * mistake as inventing a cause for a 400.
 *
 * Surfaced, never interpreted. What a non-zero value MEANS is phase 7's
 * decision, made with the probe in hand.
 */
export type DouyinStatusCode = number | null;

export interface VideoPlayAddr {
  /** Empty when Douyin returned no playable URL. Phase 6 decides what that means. */
  playAddrUrls: string[];
  statusCode: DouyinStatusCode;
}

export interface UserProfile {
  /** Null when Douyin answered without one -- never a fabricated placeholder. */
  nickname: string | null;
  statusCode: DouyinStatusCode;
}

/** Every Douyin-web route the fork exposes lives under this prefix. */
const ROUTE_PREFIX = '/api/douyin/web';

/**
 * Generous on purpose. `fetch_user_post_videos` and `get_sec_user_id` both
 * make the fork talk to Douyin (the latter follows redirects with an
 * httpx client configured for 5 transport retries), so a slow-but-alive
 * call is normal. The budget exists to stop a lane wedging forever, not to
 * enforce latency.
 */
const DEFAULT_TIMEOUT_MS = 30_000;

export class ParserClient {
  private readonly timeoutMs: number;
  /**
   * The client's OWN dispatcher, never the global one.
   *
   * `undici.request` uses a process-wide dispatcher unless handed one, and
   * anything in this process can replace it -- `setGlobalDispatcher(new
   * RetryAgent(...))` in some unrelated library turns every parser call into
   * four. That multiplies the request rate of an enumerate loop that believes
   * it issues one call every 3-5 seconds (F2), which is the ban risk this
   * whole design is arranged around. No mutation of THIS file could ever
   * catch that regression, because the change lives in another file; owning
   * the dispatcher is what makes the invariant local enough to defend.
   *
   * It also gives the client something to close, which phase 4's graceful
   * shutdown needs: a successful call leaves a keep-alive socket on the event
   * loop, and a process with one of those does not exit.
   */
  private readonly dispatcher: Dispatcher;

  constructor(private readonly options: ParserClientOptions) {
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.dispatcher =
      options.dispatcher ??
      new Agent({
        connect: { timeout: this.timeoutMs },
        headersTimeout: this.timeoutMs,
        bodyTimeout: this.timeoutMs,
      });
  }

  /** Releases sockets. Safe to call more than once. */
  async close(): Promise<void> {
    await this.dispatcher.close();
  }

  /**
   * Douyin profile URL -> `sec_user_id`.
   *
   * The URL is handed to the fork verbatim, in whatever form the operator
   * pasted it. There is deliberately NO URL parsing on this side: the fork
   * already branches on `v.douyin.com` and follows the redirect chain to
   * read `sec_uid` out of the landing URL (crawlers/douyin/web/utils.py,
   * SecUserIdFetcher.get_sec_user_id). Re-implementing that here would mean
   * owning a second, subtly different URL grammar that drifts the moment
   * Douyin adds a share-link shape -- for zero benefit, since the call has
   * to be made either way.
   */
  async resolveSecUserId(url: string): Promise<string> {
    const data = await this.get('get_sec_user_id', { url });
    if (typeof data !== 'string' || data.length === 0) {
      throw new ParserUnavailableError(
        'parser returned no sec_user_id for get_sec_user_id',
      );
    }
    return data;
  }

  /**
   * Profile metadata for a `sec_user_id`.
   *
   * Also phase 7's F4 probe: run against a KNOWN-PUBLIC profile
   * (`PROBE_SEC_USER_ID`), a success here says the cookie is still good, so
   * whatever 400 triggered the probe was about the target profile rather
   * than about us. That is why this must not throw on a thin-but-valid
   * answer -- a 200 with no nickname is still a successful probe.
   */
  async fetchUserProfile(secUserId: string): Promise<UserProfile> {
    const data = await this.get('handler_user_profile', { sec_user_id: secUserId });
    const user = pick(data, 'user');
    const nickname = pick(user, 'nickname');
    return {
      nickname: typeof nickname === 'string' ? nickname : null,
      statusCode: douyinStatusCode(data),
    };
  }

  /**
   * One page of a profile's posts.
   *
   * Items come back exactly as Douyin sent them. That is not laziness: each
   * item already carries `video.play_addr.url_list`, and caching that URL
   * (F1) is what keeps a 500-video profile at ~25 requests instead of ~525.
   * Anything that trimmed or reshaped items here would quietly force phase 6
   * back onto a `fetch_one_video` per video.
   *
   * An empty `aweme_list` is NOT an error at this layer, even with
   * `has_more` set. That pairing is phase 5's structural blocked signal and
   * must arrive there intact -- see the test.
   */
  async fetchUserPostPage(
    secUserId: string,
    maxCursor: number,
    count: number,
  ): Promise<UserPostPage> {
    const data = await this.get('fetch_user_post_videos', {
      sec_user_id: secUserId,
      max_cursor: String(maxCursor),
      count: String(count),
    });
    const list = pick(data, 'aweme_list');
    const hasMore = toBoolean(pick(data, 'has_more'));
    const cursor = toNumber(pick(data, 'max_cursor'));

    if (hasMore && cursor === null) {
      // Falling back to 0 here would mean "start again from page one", every
      // time, forever: the loop never advances and never terminates, and it
      // spends the F2 request budget doing it. A page that claims more work
      // but carries no readable cursor is unusable, and saying so beats
      // looping silently.
      throw new ParserUnavailableError(
        `parser returned a page for ${secUserId} with has_more set but no readable max_cursor`,
      );
    }

    return {
      awemeList: Array.isArray(list) ? (list as Aweme[]) : [],
      hasMore,
      maxCursor: cursor ?? 0,
      statusCode: douyinStatusCode(data),
    };
  }

  /**
   * A freshly issued play address for one video.
   *
   * Called ONLY on the re-resolve path (F1): a cached `play_addr` that has
   * expired, or that answered 403/410. Never pre-emptively, and never in a
   * loop over a profile -- doing it per video is the ~525-request pattern
   * this design exists to avoid.
   */
  async fetchOneVideo(awemeId: string): Promise<VideoPlayAddr> {
    const data = await this.get('fetch_one_video', { aweme_id: awemeId });
    const detail = pick(data, 'aweme_detail');
    return {
      playAddrUrls: playAddrUrls((detail ?? {}) as Aweme),
      statusCode: douyinStatusCode(data),
    };
  }

  /**
   * One GET, one request. No retries, ever.
   *
   * Retry policy belongs to the lane, which is the only layer that knows
   * the rate-limit budget it is spending: Douyin limits by IP, so a silent
   * retry here multiplies the request rate of an enumerate loop that
   * believes it is issuing one call every 3-5 seconds (F2). A client that
   * retries three times turns a careful crawl into a ban.
   *
   * undici's `request` does not retry on its own -- there is no RetryAgent
   * in play, and none may be added. `test/parser/parser.client.spec.ts` pins
   * this with a request counter on the fake parser.
   */
  private async get(route: string, params: Record<string, string>): Promise<unknown> {
    let statusCode: number;
    let body: unknown;
    try {
      // Inside the try: a malformed PARSER_BASE_URL makes `new URL` throw a
      // raw `TypeError [ERR_INVALID_URL]`, which is neither error class and
      // would sail straight past a lane catching ParserError. env.ts validates
      // the var as a URL so this should be unreachable; "should be" is not a
      // reason to let a different exception type out of this method.
      const target = new URL(`${ROUTE_PREFIX}/${route}`, this.options.baseUrl);
      for (const [key, value] of Object.entries(params)) {
        target.searchParams.set(key, value);
      }

      const res = await request(target, {
        method: 'GET',
        dispatcher: this.dispatcher,
        signal: AbortSignal.timeout(this.timeoutMs),
        headersTimeout: this.timeoutMs,
        bodyTimeout: this.timeoutMs,
      });
      statusCode = res.statusCode;
      // Read the body unconditionally: undici keeps the connection stuck
      // until it is consumed, and an unread error body leaks a socket per
      // failed call.
      const text = await res.body.text();
      body = safeJsonParse(text);
    } catch (cause) {
      throw new ParserUnavailableError(
        `parser did not answer GET ${ROUTE_PREFIX}/${route} at ${this.options.baseUrl} within ` +
          `${this.timeoutMs}ms, or the address could not be used at all`,
        cause,
      );
    }

    if (statusCode >= 500) {
      // Not the fork's catch-all -- the fork answers 400 for every internal
      // exception. A 5xx comes from uvicorn or whatever is in front of it,
      // meaning no Douyin-level verdict was ever reached.
      throw new ParserUnavailableError(
        `parser answered GET ${ROUTE_PREFIX}/${route} with HTTP ${statusCode}, which the fork itself never returns`,
      );
    }

    if (statusCode === 400) {
      throw new ParserRejectedError(
        statusCode,
        `parser refused GET ${ROUTE_PREFIX}/${route} with HTTP 400. The fork answers 400 for every ` +
          `internal exception and its body carries no diagnostic information at all, so the cause is ` +
          `unknown here by construction -- see src/parser/parser.errors.ts (F4).`,
      );
    }

    if (statusCode !== 200) {
      throw new ParserRejectedError(
        statusCode,
        `parser refused GET ${ROUTE_PREFIX}/${route} with HTTP ${statusCode}. That is a contract ` +
          `mismatch on this side (wrong route or wrong parameter names), not a Douyin-level refusal.`,
      );
    }

    if (!isEnvelope(body)) {
      throw new ParserUnavailableError(
        `parser answered GET ${ROUTE_PREFIX}/${route} with HTTP 200 but not the {code, router, data} ` +
          `envelope -- something other than the parser answered.`,
      );
    }

    return body.data;
  }
}

/**
 * The playable URLs Douyin attached to an aweme, best first.
 *
 * One extractor shared by both callers on purpose: enumerate reads it off
 * `aweme_list` items (F1) and the re-resolve path reads it off
 * `fetch_one_video`. Two copies would drift the first time Douyin moves the
 * field, and the second copy is always the one nobody notices is stale.
 */
export function playAddrUrls(aweme: Aweme): string[] {
  const urls = pick(pick(pick(aweme, 'video'), 'play_addr'), 'url_list');
  if (!Array.isArray(urls)) return [];
  return urls.filter((url): url is string => typeof url === 'string' && url.length > 0);
}

function douyinStatusCode(data: unknown): DouyinStatusCode {
  const raw = pick(data, 'status_code');
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw;
  if (typeof raw === 'string' && raw.trim() !== '' && Number.isFinite(Number(raw))) return Number(raw);
  return null;
}

/**
 * Douyin sends `has_more` as 1/0, and has been seen to send it as the JSON
 * strings "1"/"0" as well. `Boolean("0")` is `true`, so a plain coercion
 * turns a last page into "there is more" and the enumerate loop never stops.
 * A real boolean has to keep working too, in case Douyin ever sends one.
 */
function toBoolean(value: unknown): boolean {
  if (typeof value === 'string') return value.trim() !== '' && value.trim() !== '0';
  return Boolean(value);
}

/** Null -- never a silent 0 -- when the value cannot be read as a number. */
function toNumber(value: unknown): number | null {
  const n = typeof value === 'string' && value.trim() !== '' ? Number(value) : value;
  return typeof n === 'number' && Number.isFinite(n) ? n : null;
}

/** Reads one property off a value that may not be an object at all. */
function pick(value: unknown, key: string): unknown {
  if (typeof value !== 'object' || value === null) return undefined;
  return (value as Record<string, unknown>)[key];
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

interface Envelope {
  code?: unknown;
  router?: unknown;
  data: unknown;
}

function isEnvelope(body: unknown): body is Envelope {
  return typeof body === 'object' && body !== null && 'data' in body;
}

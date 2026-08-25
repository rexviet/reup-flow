import { ParserClient, playAddrUrls as playAddrOf } from '../../src/parser/parser.client';
import { ParserRejectedError, ParserUnavailableError } from '../../src/parser/parser.errors';
import {
  PARSER_FORWARD_URL,
  douyinCookieConfigured,
  startParserForward,
  stopParserForward,
} from '../support/parser-forward';

/**
 * ParserClient against the REAL parser container (fork @5fe4e4d).
 *
 * Kept out of `pnpm test` and run by `pnpm test:integration`. The db suites
 * also need docker, so that is not the reason -- this one needs the public
 * internet and a live Douyin. That is a different reliability class, and
 * folding it into the default suite would train everyone to shrug at a red
 * run.
 *
 * What it proves that the unit suite cannot: the routes, the parameter
 * names, and the envelope this client assumes are the ones the fork actually
 * serves.
 *
 * ON COOKIES. Only `get_sec_user_id` works without a Douyin session; it just
 * follows redirects. Every other route calls a Douyin API and, with no
 * cookie, fails with the fork's catch-all 400 -- indistinguishable from a
 * banned cookie or a private profile. That is F4, observed rather than
 * argued. So the cookie-free assertions below stop at the CONTRACT (right
 * route, right parameter names), and the data assertions are gated on a
 * cookie actually being configured.
 */
const FULL_PROFILE_URL =
  process.env.DOUYIN_TEST_PROFILE_URL ??
  'https://www.douyin.com/user/MS4wLjABAAAANXSltcLCzDGmdNFI2Q_QixVTr67NiYzjKOIP5s03CAE';
const EXPECTED_SEC_USER_ID =
  process.env.DOUYIN_TEST_SEC_USER_ID ?? 'MS4wLjABAAAANXSltcLCzDGmdNFI2Q_QixVTr67NiYzjKOIP5s03CAE';
const SAMPLE_AWEME_ID = process.env.DOUYIN_TEST_AWEME_ID ?? '7372484719365098803';

/**
 * A live `v.douyin.com/xxx/` share link. Douyin mints these per share and
 * they cannot be fabricated, so this half of the DoD is env-gated. Gated as
 * `it.failing`, never `it.skip`: a skipped test reads as "fine", and this one
 * is an open gap.
 */
const SHORT_LINK = process.env.DOUYIN_TEST_SHORT_LINK;
const HAS_COOKIE = douyinCookieConfigured();

/**
 * `it` when the prerequisite is met, otherwise `it.failing` -- the test is
 * kept and REQUIRED to throw, so removing the prerequisite check silently is
 * not an option. Jest still renders `it.failing` as a tick, so the banner
 * below is what actually makes the gap visible in a run; the DoD is not met
 * on a run that prints it.
 */
const withCookie = HAS_COOKIE ? it : it.failing;
const withShortLink = SHORT_LINK ? it : it.failing;

if (!HAS_COOKIE || !SHORT_LINK) {
  const missing = [
    HAS_COOKIE ? null : 'a Douyin cookie (redis douyin:cookie:current / DOUYIN_COOKIE_FALLBACK)',
    SHORT_LINK ? null : 'DOUYIN_TEST_SHORT_LINK (a live https://v.douyin.com/xxx/ share link)',
  ].filter(Boolean);
  process.stderr.write(
    `\n!! parser integration suite is running WITHOUT: ${missing.join(', ')}.\n` +
      `!! The assertions those unlock are recorded as expected-failures, not skips.\n` +
      `!! A green run here does NOT clear those DoD lines.\n\n`,
  );
}

describe('ParserClient against the real parser container', () => {
  let client: ParserClient;

  beforeAll(async () => {
    await startParserForward();
    client = new ParserClient({ baseUrl: PARSER_FORWARD_URL, timeoutMs: 60_000 });
  }, 120_000);

  afterAll(async () => {
    await stopParserForward();
  }, 30_000);

  describe('get_sec_user_id (the one route that needs no session)', () => {
    it('resolves a full profile URL to its sec_user_id', async () => {
      await expect(client.resolveSecUserId(FULL_PROFILE_URL)).resolves.toBe(EXPECTED_SEC_USER_ID);
    }, 90_000);

    withShortLink(
      'resolves a v.douyin.com short link with no URL parsing on this side',
      async () => {
        if (!SHORT_LINK) {
          throw new Error(
            'DOUYIN_TEST_SHORT_LINK is not set. Share any Douyin profile from the app to get a ' +
              'https://v.douyin.com/xxx/ link, export it, and re-run `pnpm test:integration`.',
          );
        }
        await expect(client.resolveSecUserId(SHORT_LINK)).resolves.toMatch(/^MS4w/);
      },
      90_000,
    );
  });

  /**
   * Route + parameter-name contract, provable WITHOUT a cookie.
   *
   * FastAPI validates the query signature before the handler body runs, so
   * the fork answers a wrong route with 404 and a wrong/missing parameter
   * name with 422. Only a request that is well-formed all the way through
   * reaches the `try:` block and comes back as the catch-all 400. So "400"
   * here is a positive statement: this client called a real route with the
   * names it really expects.
   *
   * The two negative controls below are what make that argument honest --
   * without them, "400" would just be the answer to everything.
   */
  describe('route and parameter contract', () => {
    async function statusOf(path: string, query: Record<string, string>): Promise<number> {
      const url = new URL(`/api/douyin/web/${path}`, PARSER_FORWARD_URL);
      for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);
      const res = await fetch(url, { signal: AbortSignal.timeout(60_000) });
      return res.status;
    }

    it('negative control: an unknown route is a 404, not the catch-all', async () => {
      expect(await statusOf('fetch_user_post_videos_typo', { sec_user_id: EXPECTED_SEC_USER_ID })).toBe(404);
    }, 60_000);

    it('negative control: a misnamed parameter is a 422, not the catch-all', async () => {
      expect(await statusOf('handler_user_profile', { secUserId: EXPECTED_SEC_USER_ID })).toBe(422);
    }, 60_000);

    it.each([
      ['fetchUserProfile', () => client.fetchUserProfile(EXPECTED_SEC_USER_ID)],
      ['fetchUserPostPage', () => client.fetchUserPostPage(EXPECTED_SEC_USER_ID, 0, 5)],
      ['fetchOneVideo', () => client.fetchOneVideo(SAMPLE_AWEME_ID)],
    ])('%s reaches a real route with the parameter names the fork declares', async (_name, call) => {
      const outcome = await call().then(
        () => ({ ok: true as const }),
        (err: unknown) => ({ ok: false as const, err }),
      );

      if (outcome.ok) return; // A cookie is configured and the call really worked.

      // No cookie: the request still got all the way into the handler body,
      // which is what this test is about. A 404 or 422 here would mean this
      // client is calling the fork wrong.
      expect(outcome.err).toBeInstanceOf(ParserRejectedError);
      expect((outcome.err as ParserRejectedError).status).toBe(400);
    }, 90_000);
  });

  describe('live data (needs a Douyin cookie)', () => {
    withCookie('reads a profile nickname', async () => {
      requireCookie();
      const profile = await client.fetchUserProfile(EXPECTED_SEC_USER_ID);
      expect(profile.nickname === null || typeof profile.nickname === 'string').toBe(true);
    }, 90_000);

    withCookie('reads a page whose items already carry play_addr.url_list (F1)', async () => {
      requireCookie();
      const page = await client.fetchUserPostPage(EXPECTED_SEC_USER_ID, 0, 5);

      expect(typeof page.hasMore).toBe('boolean');
      expect(typeof page.maxCursor).toBe('number');
      expect(page.awemeList.length).toBeGreaterThan(0);
      // The whole basis of F1: enumerate alone is enough to start downloading.
      expect(playAddrOf(page.awemeList[0]).length).toBeGreaterThan(0);
    }, 90_000);
  });

  it('reports an unreachable parser as ParserUnavailableError, not a generic failure', async () => {
    // The DoD line "parser off -> ParserUnavailableError", against a real
    // closed port rather than a stubbed transport.
    const offline = new ParserClient({ baseUrl: 'http://127.0.0.1:1/', timeoutMs: 3000 });

    await expect(offline.resolveSecUserId(FULL_PROFILE_URL)).rejects.toBeInstanceOf(
      ParserUnavailableError,
    );
  }, 30_000);
});

function requireCookie(): void {
  if (!HAS_COOKIE) {
    throw new Error(
      'No Douyin cookie is configured (redis douyin:cookie:current is empty and ' +
        'DOUYIN_COOKIE_FALLBACK is unset in the parser container), so every route except ' +
        'get_sec_user_id can only return the fork catch-all 400. Set one and re-run.',
    );
  }
}

import { ParserClient } from '../../src/parser/parser.client';
import { ParserRejectedError, ParserUnavailableError } from '../../src/parser/parser.errors';
import { Agent, RetryAgent, getGlobalDispatcher, setGlobalDispatcher } from 'undici';
import { playAddrUrls } from '../../src/parser/parser.client';
import { FakeParser } from '../support/fake-parser';

describe('ParserClient', () => {
  const parser = new FakeParser();
  let client: ParserClient;

  beforeAll(async () => {
    await parser.start();
  });

  afterAll(async () => {
    await parser.stop();
  });

  beforeEach(() => {
    parser.reset();
    client = new ParserClient({ baseUrl: parser.baseUrl, timeoutMs: 2000 });
  });

  describe('resolveSecUserId', () => {
    it('unwraps the {code, router, data} envelope and returns data', async () => {
      parser.respondWithEnvelope('MS4wLjABAAAA_from_envelope');

      await expect(
        client.resolveSecUserId('https://www.douyin.com/user/MS4wLjABAAAA_from_envelope'),
      ).resolves.toBe('MS4wLjABAAAA_from_envelope');
    });
  });

  describe('error classification (F4)', () => {
    it('maps the fork catch-all 400 to ParserRejectedError carrying the status', async () => {
      parser.respondWithParserError(400);

      const err = await client.resolveSecUserId('https://www.douyin.com/user/x').catch((e) => e);

      expect(err).toBeInstanceOf(ParserRejectedError);
      expect((err as ParserRejectedError).status).toBe(400);
    });

    it('never claims a cause for a 400 -- the parser does not know one', async () => {
      parser.respondWithParserError(400);

      const err: Error = await client.resolveSecUserId('https://www.douyin.com/user/x').catch((e) => e);

      // The whole point of F4: a 400 is information-free. If this message ever
      // starts naming a cause, phase 7's probe has been pre-empted by a guess.
      for (const invented of ['cookie', 'banned', 'blocked', 'private', 'expired', 'rate limit']) {
        expect(err.message.toLowerCase()).not.toContain(invented);
      }
    });

    it('does not mistake the parser\'s static "An error occurred." for a diagnosis', async () => {
      parser.respondWithParserError(400);

      const err: Error = await client.resolveSecUserId('https://www.douyin.com/user/x').catch((e) => e);

      expect(err.message).toMatch(/no diagnostic information|cause is unknown/i);
    });

    it('maps a refused connection to ParserUnavailableError', async () => {
      const dead = new ParserClient({ baseUrl: 'http://127.0.0.1:1/', timeoutMs: 2000 });

      await expect(dead.resolveSecUserId('https://www.douyin.com/user/x')).rejects.toBeInstanceOf(
        ParserUnavailableError,
      );
    });

    it('maps a 5xx to ParserUnavailableError, not a rejection', async () => {
      parser.respondWith((_req, res) => res.writeHead(502).end('bad gateway'));

      await expect(client.resolveSecUserId('https://www.douyin.com/user/x')).rejects.toBeInstanceOf(
        ParserUnavailableError,
      );
    });

    it('maps a 200 that is not the envelope to ParserUnavailableError', async () => {
      parser.respondWith((_req, res) => {
        res.writeHead(200, { 'content-type': 'text/html' });
        res.end('<html>captive portal</html>');
      });

      await expect(client.resolveSecUserId('https://www.douyin.com/user/x')).rejects.toBeInstanceOf(
        ParserUnavailableError,
      );
    });

    it('keeps a route/param mismatch distinguishable from the catch-all 400', async () => {
      // 404 and 422 mean this client is calling the fork wrong. Phase 7 must be
      // able to see that without probing Douyin for a cookie ban that is not
      // there -- hence `status` on the error rather than a second class.
      parser.respondWith((_req, res) => res.writeHead(404).end('{"detail":"Not Found"}'));

      const err = await client.resolveSecUserId('https://www.douyin.com/user/x').catch((e) => e);

      expect(err).toBeInstanceOf(ParserRejectedError);
      expect((err as ParserRejectedError).status).toBe(404);
    });

    it('times out instead of hanging forever, and reports it as unavailable', async () => {
      parser.respondNever();
      const impatient = new ParserClient({ baseUrl: parser.baseUrl, timeoutMs: 150 });

      await expect(impatient.resolveSecUserId('https://www.douyin.com/user/x')).rejects.toBeInstanceOf(
        ParserUnavailableError,
      );
    });
  });

  describe('fetchUserProfile', () => {
    it('returns the nickname out of the raw Douyin user object', async () => {
      parser.respondWithEnvelope({ status_code: 0, user: { nickname: '小明', sec_uid: 'MS4w' } });

      await expect(client.fetchUserProfile('MS4w')).resolves.toEqual({ nickname: '小明', statusCode: 0 });
    });

    it('calls handler_user_profile with sec_user_id', async () => {
      parser.respondWithEnvelope({ user: { nickname: 'x' } });

      await client.fetchUserProfile('MS4wLjABAAAA_abc');

      expect(parser.requests).toHaveLength(1);
      expect(parser.requests[0].path).toBe('/api/douyin/web/handler_user_profile');
      expect(parser.requests[0].query.get('sec_user_id')).toBe('MS4wLjABAAAA_abc');
    });

    it('reports a missing nickname as null rather than inventing one', async () => {
      // Phase 7 uses this call as a liveness probe; a profile that answers 200
      // with no nickname is still a successful probe, so this must not throw.
      parser.respondWithEnvelope({ status_code: 0, user: {} });

      await expect(client.fetchUserProfile('MS4w')).resolves.toEqual({ nickname: null, statusCode: 0 });
    });
  });

  describe('fetchUserPostPage', () => {
    const page = {
      status_code: 0,
      aweme_list: [
        { aweme_id: '111', video: { play_addr: { url_list: ['https://a/1', 'https://b/1'] } } },
        { aweme_id: '222', video: { play_addr: { url_list: ['https://a/2'] } } },
      ],
      has_more: 1,
      max_cursor: 1724500000000,
    };

    it('maps the page to {awemeList, hasMore, maxCursor}', async () => {
      parser.respondWithEnvelope(page);

      const result = await client.fetchUserPostPage('MS4w', 0, 20);

      expect(result.hasMore).toBe(true);
      expect(result.maxCursor).toBe(1724500000000);
      expect(result.awemeList).toHaveLength(2);
    });

    it('hands back aweme items untouched so play_addr.url_list survives (F1)', async () => {
      parser.respondWithEnvelope(page);

      const result = await client.fetchUserPostPage('MS4w', 0, 20);

      // F1 rests entirely on this: the enumerate response ALREADY carries a
      // playable URL, which is what makes 25 requests enough for a 500-video
      // profile instead of 525. Reshaping or trimming items here would push
      // phase 6 back onto fetch_one_video per video.
      expect(result.awemeList[0]).toEqual(page.aweme_list[0]);
      expect(playAddrUrls(result.awemeList[0])).toEqual(['https://a/1', 'https://b/1']);
    });

    it('sends sec_user_id, max_cursor and count as the fork names them', async () => {
      parser.respondWithEnvelope(page);

      await client.fetchUserPostPage('MS4wLjABAAAA_abc', 1724500000000, 20);

      expect(parser.requests[0].path).toBe('/api/douyin/web/fetch_user_post_videos');
      expect(parser.requests[0].query.get('sec_user_id')).toBe('MS4wLjABAAAA_abc');
      expect(parser.requests[0].query.get('max_cursor')).toBe('1724500000000');
      expect(parser.requests[0].query.get('count')).toBe('20');
    });

    it('coerces Douyin\'s numeric has_more to a boolean in both directions', async () => {
      parser.respondWithEnvelope({ ...page, has_more: 0 });
      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toMatchObject({ hasMore: false });

      parser.respondWithEnvelope({ ...page, has_more: true });
      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toMatchObject({ hasMore: true });
    });

    it('passes an empty page with has_more=1 straight through instead of throwing', async () => {
      // This exact combination is phase 5's STRUCTURAL blocked signal: HTTP
      // 200, so nothing failed, yet Douyin returned nothing while claiming
      // there is more. It is the one blocked signal that is certain rather
      // than inferred (plan §6), and it only survives if this layer refuses
      // to treat an empty list as an error.
      parser.respondWithEnvelope({ status_code: 0, aweme_list: [], has_more: 1, max_cursor: 42 });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toEqual({
        awemeList: [],
        hasMore: true,
        maxCursor: 42,
        statusCode: 0,
      });
    });

    it('treats a page with no aweme_list at all as an empty page, not a crash', async () => {
      parser.respondWithEnvelope({ status_code: 0, has_more: 0, max_cursor: 0 });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toEqual({
        awemeList: [],
        hasMore: false,
        maxCursor: 0,
        statusCode: 0,
      });
    });
  });

  describe('fetchOneVideo', () => {
    it('returns the freshly issued play_addr urls', async () => {
      parser.respondWithEnvelope({
        status_code: 0,
        aweme_detail: {
          aweme_id: '111',
          video: { play_addr: { url_list: ['https://fresh/1', 'https://fresh/2'] } },
        },
      });

      await expect(client.fetchOneVideo('111')).resolves.toEqual({
        playAddrUrls: ['https://fresh/1', 'https://fresh/2'],
        statusCode: 0,
      });
    });

    it('calls fetch_one_video with aweme_id', async () => {
      parser.respondWithEnvelope({ aweme_detail: {} });

      await client.fetchOneVideo('7372484719365098803');

      expect(parser.requests[0].path).toBe('/api/douyin/web/fetch_one_video');
      expect(parser.requests[0].query.get('aweme_id')).toBe('7372484719365098803');
    });

    it('returns an empty url list rather than throwing when Douyin sends none', async () => {
      // Phase 6 decides what an unplayable video means (skip vs fail). This
      // layer only reports what came back.
      parser.respondWithEnvelope({ aweme_detail: { video: {} } });

      await expect(client.fetchOneVideo('111')).resolves.toEqual({ playAddrUrls: [], statusCode: null });
    });
  });

  describe('no retries (F2 budget)', () => {
    // Douyin rate-limits by IP, so a retry hidden in this client multiplies
    // the request rate of an enumerate loop that believes it issues one call
    // every 3-5 seconds. Retry policy belongs to the lane, which is the only
    // layer that knows the budget it is spending.
    it.each([
      ['a 400 from the fork catch-all', 400],
      ['a 502 from in front of the fork', 502],
      ['a 404 route mismatch', 404],
    ])('makes exactly one request for %s', async (_label, status) => {
      parser.respondWith((_req, res) => res.writeHead(status).end('{}'));

      await client.resolveSecUserId('https://www.douyin.com/user/x').catch(() => undefined);

      expect(parser.requests).toHaveLength(1);
    });

    it('makes exactly one request on a success too', async () => {
      parser.respondWithEnvelope('MS4w');

      await client.resolveSecUserId('https://www.douyin.com/user/x');

      expect(parser.requests).toHaveLength(1);
    });
  });

  describe('URL handling is the fork\'s job, not ours', () => {
    it.each([
      ['a full profile URL', 'https://www.douyin.com/user/MS4wLjABAAAANXSltcLCzDGmdNFI2Q_QixVTr67NiYzjKOIP5s03CAE'],
      ['a share short link', 'https://v.douyin.com/iRNBho6G/'],
      ['a short link with tracking junk', 'https://v.douyin.com/iRNBho6G/?u=1&from=copy_link'],
    ])('forwards %s verbatim to get_sec_user_id', async (_label, url) => {
      parser.respondWithEnvelope('MS4w');

      await client.resolveSecUserId(url);

      expect(parser.requests).toHaveLength(1);
      expect(parser.requests[0].path).toBe('/api/douyin/web/get_sec_user_id');
      expect(parser.requests[0].query.get('url')).toBe(url);
    });

    it('rejects an answer with no sec_user_id instead of returning an empty string', async () => {
      parser.respondWithEnvelope('');

      await expect(client.resolveSecUserId('https://v.douyin.com/x/')).rejects.toBeInstanceOf(
        ParserUnavailableError,
      );
    });
  });

  describe('Douyin-level status_code survives the unwrap (F4, the other direction)', () => {
    // The fork returns ANY Douyin 200 body verbatim and wraps it in its own
    // `code: 200` envelope (crawlers/base_crawler.py parse_json). So a refusal
    // at Douyin's application layer -- the shape a banned cookie produces --
    // arrives here as a perfectly healthy HTTP 200.
    //
    // Phase 7 runs fetchUserProfile against a KNOWN-PUBLIC profile and reads
    // success as "the cookie is still good". If this layer discards
    // status_code, that probe reports a refused cookie as healthy and sends
    // the operator to investigate the profile instead. The plan already
    // concedes 200 does not mean success: phase 5's structural blocked signal
    // is an empty aweme_list with has_more=1, at HTTP 200.
    //
    // Surfaced, NOT interpreted -- deciding what a non-zero value means is
    // phase 7's job, exactly as with the 400.
    it('surfaces status_code from a profile answer instead of dropping it', async () => {
      parser.respondWithEnvelope({ status_code: 8, status_msg: 'blocked', user: null });

      await expect(client.fetchUserProfile('MS4w')).resolves.toEqual({
        nickname: null,
        statusCode: 8,
      });
    });

    it('reports the success value on a healthy profile answer', async () => {
      parser.respondWithEnvelope({ status_code: 0, user: { nickname: 'ok' } });

      await expect(client.fetchUserProfile('MS4w')).resolves.toEqual({
        nickname: 'ok',
        statusCode: 0,
      });
    });

    it('reports a missing status_code as null rather than as success', async () => {
      // Inventing 0 here would be the same class of mistake as inventing a
      // cause for a 400: asserting health that was never observed.
      parser.respondWithEnvelope({ user: { nickname: 'ok' } });

      await expect(client.fetchUserProfile('MS4w')).resolves.toEqual({
        nickname: 'ok',
        statusCode: null,
      });
    });

    it('surfaces status_code from a page answer', async () => {
      parser.respondWithEnvelope({ status_code: 8, aweme_list: [], has_more: 0, max_cursor: 0 });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toMatchObject({ statusCode: 8 });
    });

    it('surfaces status_code from a single-video answer', async () => {
      parser.respondWithEnvelope({ status_code: 8, aweme_detail: null });

      await expect(client.fetchOneVideo('111')).resolves.toEqual({
        playAddrUrls: [],
        statusCode: 8,
      });
    });
  });

  describe('paging values that would silently wedge the enumerate loop', () => {
    it('reads has_more="0" as false, not as a non-empty string', async () => {
      // Douyin has been seen to send these as JSON strings. `Boolean("0")` is
      // true, so a naive coercion says "there is more" forever.
      parser.respondWithEnvelope({ aweme_list: [], has_more: '0', max_cursor: 0 });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toMatchObject({ hasMore: false });
    });

    it('reads has_more="1" as true', async () => {
      parser.respondWithEnvelope({ aweme_list: [], has_more: '1', max_cursor: 7 });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toMatchObject({ hasMore: true });
    });

    it('refuses a page that claims more but carries no usable cursor', async () => {
      // Falling back to 0 here means "restart from page one", forever: the
      // loop never terminates and never advances, burning the F2 request
      // budget that the whole design exists to protect. A cursor we cannot
      // read makes the page unusable, and saying so is better than looping.
      parser.respondWithEnvelope({ aweme_list: [{}], has_more: 1, max_cursor: 'lol' });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).rejects.toBeInstanceOf(
        ParserUnavailableError,
      );
    });

    it('accepts a missing cursor on the last page, where it cannot loop', async () => {
      parser.respondWithEnvelope({ aweme_list: [{}], has_more: 0 });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toMatchObject({
        hasMore: false,
        maxCursor: 0,
      });
    });

    it('accepts a numeric string cursor', async () => {
      parser.respondWithEnvelope({ aweme_list: [{}], has_more: 1, max_cursor: '1724500000000' });

      await expect(client.fetchUserPostPage('MS4w', 0, 20)).resolves.toMatchObject({
        maxCursor: 1724500000000,
      });
    });
  });

  describe('the no-retry rule must hold against the rest of the process', () => {
    it('ignores a RetryAgent someone else installed as the global dispatcher', async () => {
      // undici's `request` uses the GLOBAL dispatcher unless given one. Any
      // library in this process can call setGlobalDispatcher(new RetryAgent(...)),
      // and every parser call would silently become four -- multiplying the
      // request rate of an enumerate loop that believes it issues one call
      // every 3-5 seconds (F2). No mutation of parser.client.ts can catch a
      // regression that lives in another file, so the client owns its own
      // dispatcher.
      const previous = getGlobalDispatcher();
      const retrying = new RetryAgent(new Agent(), { maxRetries: 3, minTimeout: 1, maxTimeout: 2 });
      setGlobalDispatcher(retrying);
      parser.respondWith((_req, res) => res.writeHead(500).end('{}'));

      try {
        await client.resolveSecUserId('https://www.douyin.com/user/x').catch(() => undefined);
        expect(parser.requests).toHaveLength(1);
      } finally {
        setGlobalDispatcher(previous);
        await retrying.close();
      }
    });

    it('can be closed, so a lane shutdown does not leave a keep-alive socket behind', async () => {
      parser.respondWithEnvelope('MS4w');
      const closable = new ParserClient({ baseUrl: parser.baseUrl, timeoutMs: 2000 });
      await closable.resolveSecUserId('https://www.douyin.com/user/x');

      await expect(closable.close()).resolves.toBeUndefined();
    });
  });

  describe('configuration errors do not escape as raw TypeErrors', () => {
    it('reports an unusable base URL as a parser error, not ERR_INVALID_URL', async () => {
      // env.ts validates PARSER_BASE_URL as a URL, so this should be
      // unreachable -- but a lane catching ParserError must not be bypassed by
      // a stray TypeError if it ever is.
      const misconfigured = new ParserClient({ baseUrl: 'parser:80', timeoutMs: 500 });

      await expect(misconfigured.resolveSecUserId('https://www.douyin.com/user/x')).rejects.toBeInstanceOf(
        ParserUnavailableError,
      );
    });
  });
});

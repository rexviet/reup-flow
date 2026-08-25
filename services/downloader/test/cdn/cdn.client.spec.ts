/**
 * Phase 6 -- the CDN port (§6.2 step 4).
 *
 * `*.douyinvod.com` is a different animal from the API the parser talks to: no
 * risk control, no cookie, no envelope. What it does have is a SIGNED url that
 * expires, and two headers it will not serve without. So this port is small on
 * purpose, and every one of its few decisions is pinned here.
 *
 * The suite drives a real HTTP server (`FakeCdn`). "One GET per attempt" and
 * "a dropped connection is not a complete download" are properties of an actual
 * exchange; a stubbed transport would let a hidden retry loop pass unnoticed,
 * which is exactly the regression `ParserClient` already carries a counter for.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { UndiciCdnClient, DOUYIN_REFERER, DOUYIN_WEB_USER_AGENT } from '../../src/cdn/cdn.client';
import { FakeCdn } from '../support/fake-cdn';

jest.setTimeout(30_000);

const cdn = new FakeCdn();
let client: UndiciCdnClient;

beforeAll(async () => {
  await cdn.start();
});

afterAll(async () => {
  await cdn.stop();
});

beforeEach(() => {
  cdn.reset();
  client = new UndiciCdnClient({ timeoutMs: 5_000 });
});

afterEach(async () => {
  await client.close();
});

/** Reads the whole body, as the storage layer would. */
async function drain(stream: NodeJS.ReadableStream): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks);
}

describe('headers', () => {
  it('sends the Douyin Referer -- the CDN refuses without it', async () => {
    cdn.serveBytes(Buffer.alloc(16, 1));

    const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);
    await drain(res.body);

    expect(cdn.downloads).toHaveLength(1);
    expect(cdn.downloads[0]!.headers.referer).toBe('https://www.douyin.com/');
    expect(DOUYIN_REFERER).toBe('https://www.douyin.com/');
  });

  it('sends the SAME User-Agent the parser fork signs its requests with', async () => {
    // "UA khớp cookie" (§6.2 step 4). The cookie was minted for a browser, and
    // the fork's douyin/web crawler sends one specific UA with it -- with a
    // comment in its own config telling you not to change it. Downloading with
    // a different UA than the one that resolved the URL is the kind of mismatch
    // a CDN is entitled to refuse, and it would look like an expired URL.
    //
    // Pinned to the fork's config FILE rather than to a copy of the string, so
    // an upstream bump that changes it fails here instead of in production.
    const configPath = resolve(
      __dirname,
      '../../../../src/Douyin_TikTok_Download_API/crawlers/douyin/web/config.yaml',
    );
    const config = parseYaml(readFileSync(configPath, 'utf8')) as {
      TokenManager: { douyin: { headers: { 'User-Agent': string } } };
    };
    const forkUserAgent = config.TokenManager.douyin.headers['User-Agent'];

    expect(DOUYIN_WEB_USER_AGENT).toBe(forkUserAgent);

    cdn.serveBytes(Buffer.alloc(16, 1));
    const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);
    await drain(res.body);

    expect(cdn.downloads[0]!.headers['user-agent']).toBe(forkUserAgent);
  });
});

describe('a successful download', () => {
  it('reports the status, the advertised length, and streams the bytes', async () => {
    const payload = Buffer.alloc(70_000, 0x5a);
    cdn.serveBytes(payload);

    const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);

    expect(res.status).toBe(200);
    expect(res.contentLength).toBe(payload.length);
    expect(await drain(res.body)).toEqual(payload);
  });

  it('makes EXACTLY ONE request -- retry policy belongs to the lane', async () => {
    // Same rule as `ParserClient.get`. A silent retry here would multiply the
    // request count the F1 arithmetic (525 vs 25) is built on, and no mutation
    // of the lane could catch it.
    cdn.serveBytes(Buffer.alloc(8, 1));

    const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);
    await drain(res.body);

    expect(cdn.downloads).toHaveLength(1);
  });

  it('reports contentLength as undefined when the CDN advertises none', async () => {
    // Never 0, and never invented. A fabricated 0 would make the storage layer
    // reject every body as "0 bytes were advertised"; a fabricated length would
    // be a truncation check against a number nobody sent.
    cdn.respondWith((_req, res) => {
      res.writeHead(200, { 'content-type': 'video/mp4', 'transfer-encoding': 'chunked' });
      res.end(Buffer.alloc(32, 3));
    });

    const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);
    await drain(res.body);

    expect(res.contentLength).toBeUndefined();
  });
});

describe('a refusal', () => {
  it.each([403, 410])('returns %i rather than throwing, so the lane can re-resolve', async (status) => {
    // F1's reactive half. A 403 is not an error to report, it is an instruction
    // to go and get a new URL -- so the port must hand the status back instead
    // of collapsing it into an exception the lane has to reverse-engineer.
    cdn.serveRefusal(status);

    const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);

    expect(res.status).toBe(status);
    await res.discard();
  });

  it('releases the connection when the body is discarded unread', async () => {
    // undici keeps a connection pinned until its body is consumed. A refusal
    // whose body is never read leaks one socket per expired URL, and a profile
    // is hundreds of videos.
    cdn.serveRefusal(403);

    for (let i = 0; i < 8; i += 1) {
      const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);
      await res.discard();
    }

    // Nothing hung: every request was answered, which it could not have been
    // with the pool exhausted.
    expect(cdn.downloads).toHaveLength(8);
  });
});

describe('a body that stops early', () => {
  it('errors the stream rather than ending it quietly', async () => {
    // The real shape of a dropped connection. Ending quietly is the dangerous
    // outcome: a short file that looks complete. Note this is caught by the
    // TRANSPORT, which is why the byte-count check in storage has its own test
    // with a source that ends cleanly -- two mechanisms, two tests.
    const payload = Buffer.alloc(64 * 1024, 7);
    cdn.serveTruncated(payload, 4096);

    const res = await client.get(cdn.url('/v.mp4'), new AbortController().signal);

    await expect(drain(res.body)).rejects.toThrow();
  });
});

describe('abort', () => {
  it('gives up when the signal fires, instead of holding shutdown open', async () => {
    // The lane hands `runOnce`'s signal down here so a SIGTERM mid-download
    // does not wait out a stalled socket -- §4.2's drain timeout is a backstop,
    // not the mechanism.
    cdn.respondNever();
    const aborter = new AbortController();
    const pending = client.get(cdn.url('/v.mp4'), aborter.signal);
    setTimeout(() => aborter.abort(), 50);

    await expect(pending).rejects.toThrow();
  });

  it('aborts a download that has already started streaming', async () => {
    const payload = Buffer.alloc(256 * 1024, 4);
    cdn.serveGated(payload, new Promise(() => {}));
    const aborter = new AbortController();

    const res = await client.get(cdn.url('/v.mp4'), aborter.signal);
    setTimeout(() => aborter.abort(), 50);

    await expect(drain(res.body)).rejects.toThrow();
  });
});

describe('an unusable address', () => {
  it('reports a connection failure as an error, not as a status', async () => {
    // Nothing answered, so there is no status to report and pretending there is
    // one (a synthetic 0, or 503) would send the lane down the re-resolve path
    // for a problem re-resolving cannot fix.
    await expect(
      client.get('http://127.0.0.1:1/v.mp4', new AbortController().signal),
    ).rejects.toThrow();
  });

  it('reports a malformed URL as an error of its own', async () => {
    // `play_addr` comes off Douyin. `new URL` throws a raw TypeError, which is
    // not the error class any lane handler matches -- the same trap
    // `ParserClient.get` documents.
    await expect(
      client.get('not-a-url-at-all', new AbortController().signal),
    ).rejects.toThrow();
  });
});

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  listJobs,
  createJob,
  cancelJob,
  retryJob,
  getCookieStatus,
  ApiError,
  getDashboardVideos,
  runAction,
  getMediaLinks,
  createVideo,
  setAutoLocalize,
  listCollections,
  createCollection,
  updateCollection,
  getGlossary,
  putGlossaryTerm,
  deleteGlossaryTerm,
  moveCollection,
} from './client';

const originalFetch = global.fetch;

beforeEach(() => {
  vi.stubEnv('VITE_API_BASE_URL', 'http://api.test');
  vi.stubEnv('VITE_API_KEY', 'test-key-1234567890');
});

afterEach(() => {
  global.fetch = originalFetch;
  vi.unstubAllEnvs();
});

function mockFetchOnce(body: unknown, init?: ResponseInit) {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status: 200, ...init, headers: { 'content-type': 'application/json' } }),
  );
  global.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

describe('listJobs', () => {
  it('GETs /api/jobs with the X-Api-Key header and returns the parsed list', async () => {
    const payload = { items: [], total: 0, limit: 20, offset: 0 };
    const fetchMock = mockFetchOnce(payload);

    const result = await listJobs();

    expect(result).toEqual(payload);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('http://api.test/api/jobs?limit=20&offset=0');
    expect((opts as RequestInit).headers).toMatchObject({ 'X-Api-Key': 'test-key-1234567890' });
  });
});

describe('createJob', () => {
  it('POSTs the url and returns job_id', async () => {
    const fetchMock = mockFetchOnce({ job_id: 'abc-123' }, { status: 201 });

    const result = await createJob('https://www.douyin.com/user/xyz');

    expect(result).toEqual({ job_id: 'abc-123' });
    const [url, opts] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('http://api.test/api/jobs');
    const init = opts as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ url: 'https://www.douyin.com/user/xyz' });
  });

  it('throws ApiError with the server message on 400', async () => {
    mockFetchOnce({ message: 'url must be an http(s) URL on one of: douyin.com, iesdouyin.com' }, { status: 400 });

    await expect(createJob('not-a-url')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('cancelJob / retryJob', () => {
  it('POSTs /api/jobs/:id/cancel', async () => {
    const fetchMock = mockFetchOnce({ job_id: 'j1', status: 'cancelled' });
    await cancelJob('j1');
    const [url, opts] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('http://api.test/api/jobs/j1/cancel');
    expect((opts as RequestInit).method).toBe('POST');
  });

  it('POSTs /api/jobs/:id/retry', async () => {
    const fetchMock = mockFetchOnce({ job: { job_id: 'j1', status: 'pending' }, videos_reset: 3 });
    await retryJob('j1');
    const [url, opts] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('http://api.test/api/jobs/j1/retry');
    expect((opts as RequestInit).method).toBe('POST');
  });
});

describe('getCookieStatus', () => {
  it('GETs /internal/cookie/status', async () => {
    const payload = { present: true, age_seconds: 120, last_ok_at: null };
    const fetchMock = mockFetchOnce(payload);

    const result = await getCookieStatus();

    expect(result).toEqual(payload);
    const [url] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('http://api.test/internal/cookie/status');
  });
});

describe('dashboard client', () => {
  it('sends only the filters that are set, as a query string', async () => {
    const fetchMock = mockFetchOnce({ items: [] });
    await getDashboardVideos({ stage: 'held', q: '', channel: 'sec1', limit: 50, offset: 0 });
    expect(String(fetchMock.mock.calls[0][0])).toBe('http://api.test/api/dashboard/videos?stage=held&channel=sec1&limit=50&offset=0');
  });

  it('posts an action body as JSON', async () => {
    const fetchMock = mockFetchOnce({ done: ['1'], skipped: [] });
    await runAction({ action: 'rerun', aweme_ids: ['1'] });
    const [url, opts] = fetchMock.mock.calls[0];
    expect(String(url)).toBe('http://api.test/api/dashboard/actions');
    expect((opts as RequestInit).method).toBe('POST');
    expect(JSON.parse(String((opts as RequestInit).body))).toEqual({ action: 'rerun', aweme_ids: ['1'] });
  });

  it('prefixes signed media urls with the API base', async () => {
    mockFetchOnce({ links: [{ kind: 'source', available: true, url: '/api/dashboard/media/tok' }, { kind: 'srt', available: false }] });
    const links = await getMediaLinks('1', 2, ['source', 'srt']);
    expect(links[0].url).toBe('http://api.test/api/dashboard/media/tok');
    expect(links[1].url).toBeUndefined();
  });

  it('uses same-origin paths when no base URL is configured', async () => {
    vi.stubEnv('VITE_API_BASE_URL', '');
    const fetchMock = mockFetchOnce({ aweme_id: '1', status: 'pending' });
    await createVideo('https://www.douyin.com/video/1', true);
    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/videos');
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({ url: 'https://www.douyin.com/video/1', localize: true });
  });

  it('omits auto_localize unless given, and patches a channel switch', async () => {
    let fetchMock = mockFetchOnce({ job_id: 'a' });
    await createJob('https://www.douyin.com/user/x');
    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({ url: 'https://www.douyin.com/user/x' });
    fetchMock = mockFetchOnce({});
    await setAutoLocalize('sec 1', true);
    expect(String(fetchMock.mock.calls[0][0])).toBe('http://api.test/api/profiles/sec%201');
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('PATCH');
  });
});

describe('collections client', () => {
  const COLLECTION = '11111111-2222-4333-8444-555555555555';
  const bodyOf = (fetchMock: ReturnType<typeof mockFetchOnce>) => JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));

  it('leaves collection_id out of a new job or video unless one is picked (the API rejects null)', async () => {
    let fetchMock = mockFetchOnce({ job_id: 'a' });
    await createJob('https://www.douyin.com/user/x', undefined, undefined);
    expect(bodyOf(fetchMock)).not.toHaveProperty('collection_id');
    fetchMock = mockFetchOnce({ job_id: 'a' });
    await createJob('https://www.douyin.com/user/x', true, COLLECTION);
    expect(bodyOf(fetchMock)).toEqual({ url: 'https://www.douyin.com/user/x', auto_localize: true, collection_id: COLLECTION });

    fetchMock = mockFetchOnce({ aweme_id: '1', status: 'pending' });
    await createVideo('https://www.douyin.com/video/1', undefined, undefined);
    expect(bodyOf(fetchMock)).toEqual({ url: 'https://www.douyin.com/video/1' });
    fetchMock = mockFetchOnce({ aweme_id: '1', status: 'pending' });
    await createVideo('https://www.douyin.com/video/1', undefined, COLLECTION);
    expect(bodyOf(fetchMock)).toEqual({ url: 'https://www.douyin.com/video/1', collection_id: COLLECTION });
  });

  it('filters the video list by collection', async () => {
    const fetchMock = mockFetchOnce({ items: [] });
    await getDashboardVideos({ collection: COLLECTION, limit: 50, offset: 0 });
    expect(String(fetchMock.mock.calls[0][0])).toBe(`http://api.test/api/dashboard/videos?collection=${COLLECTION}&limit=50&offset=0`);
  });

  it('lists, creates and patches collections', async () => {
    let fetchMock = mockFetchOnce({ items: [] });
    await listCollections();
    expect(String(fetchMock.mock.calls[0][0])).toBe('http://api.test/api/collections');

    fetchMock = mockFetchOnce({ id: COLLECTION }, { status: 201 });
    await createCollection({ name: 'Phim A' });
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('POST');
    expect(bodyOf(fetchMock)).toEqual({ name: 'Phim A' });

    fetchMock = mockFetchOnce({ id: COLLECTION });
    await updateCollection(COLLECTION, { caption_text_color: null, cover_opacity_pct: 40 });
    expect(String(fetchMock.mock.calls[0][0])).toBe(`http://api.test/api/collections/${COLLECTION}`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('PATCH');
    expect(bodyOf(fetchMock)).toEqual({ caption_text_color: null, cover_opacity_pct: 40 });
  });

  it('puts and deletes a glossary term under its encoded Chinese key', async () => {
    let fetchMock = mockFetchOnce({ items: [] });
    await getGlossary(COLLECTION);
    expect(String(fetchMock.mock.calls[0][0])).toBe(`http://api.test/api/collections/${COLLECTION}/glossary`);

    fetchMock = mockFetchOnce({ term_zh: '成名/代价' });
    await putGlossaryTerm(COLLECTION, '成名/代价', { term_vi: 'Cái giá', note: null });
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      `http://api.test/api/collections/${COLLECTION}/glossary/${encodeURIComponent('成名/代价')}`,
    );
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('PUT');
    expect(bodyOf(fetchMock)).toEqual({ term_vi: 'Cái giá', note: null });

    fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    global.fetch = fetchMock as unknown as typeof fetch;
    await expect(deleteGlossaryTerm(COLLECTION, '成名')).resolves.toBeUndefined();
    expect(String(fetchMock.mock.calls[0][0])).toBe(`http://api.test/api/collections/${COLLECTION}/glossary/${encodeURIComponent('成名')}`);
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('DELETE');
  });

  it('moves videos with a PATCH', async () => {
    const fetchMock = mockFetchOnce({ done: ['1'], skipped: [] });
    await moveCollection(['1', '2'], COLLECTION);
    expect(String(fetchMock.mock.calls[0][0])).toBe('http://api.test/api/dashboard/videos/collection');
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('PATCH');
    expect(bodyOf(fetchMock)).toEqual({ aweme_ids: ['1', '2'], collection_id: COLLECTION });
  });

  it('keeps each validation message of a 400 whose message is a list', async () => {
    mockFetchOnce({ message: ['term_vi: term_vi is empty', 'note: too long'], error: 'Bad Request', statusCode: 400 }, { status: 400 });
    const err = await putGlossaryTerm(COLLECTION, 'x', { term_vi: '' }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).messages).toEqual(['term_vi: term_vi is empty', 'note: too long']);
    expect((err as ApiError).message).toBe('term_vi: term_vi is empty; note: too long');
  });
});

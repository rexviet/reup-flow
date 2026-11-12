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

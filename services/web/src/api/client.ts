import type {
  ActionRequest,
  ActionResult,
  AttentionResponse,
  AutoQueueStatus,
  ChannelListResponse,
  CookieStatus,
  HealthReport,
  JobListResponse,
  JobResponse,
  MediaKind,
  MediaLink,
  VideoDetailResponse,
  VideoListQuery,
  VideoListResponse,
} from './types';

/**
 * Thin fetch wrapper for the downloader API (services/downloader).
 *
 * `X-Api-Key` header name matches `API_KEY_HEADER` in
 * services/downloader/src/auth/api-key.guard.ts verbatim -- every route in
 * that service is behind the global `ApiKeyGuard`, `/internal/*` included.
 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/** Empty (the default build) means same origin: every request goes through the web nginx. */
export function baseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${baseUrl()}${path}`, {
    ...init,
    headers: {
      'X-Api-Key': import.meta.env.VITE_API_KEY,
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...init?.headers,
    },
  });

  if (!res.ok) {
    // Server error bodies are `{ message: string }` (Nest's default
    // exception filter) for every 4xx/5xx this API raises -- see
    // jobs.controller.ts's mapParserError and the various
    // BadRequestException/ConflictException/NotFoundException throws across
    // jobs.controller.ts and cookie.controller.ts. Fall back to the status
    // text if the body isn't JSON (a proxy error page, a dead upstream).
    let message = res.statusText;
    try {
      const body = (await res.json()) as { message?: string };
      if (typeof body.message === 'string') message = body.message;
    } catch {
      // not JSON -- keep statusText
    }
    throw new ApiError(res.status, message);
  }

  // 200s with no body would break `.json()`; none of the routes this client
  // calls today return an empty body, but guard anyway rather than assume.
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export async function listJobs(limit = 20, offset = 0): Promise<JobListResponse> {
  return request<JobListResponse>(`/api/jobs?limit=${limit}&offset=${offset}`);
}

export async function createJob(url: string, autoLocalize?: boolean): Promise<{ job_id: string }> {
  return request<{ job_id: string }>('/api/jobs', {
    method: 'POST',
    body: JSON.stringify(autoLocalize === undefined ? { url } : { url, auto_localize: autoLocalize }),
  });
}

/** One video by its page URL; `localize: true` skips the hold. */
export async function createVideo(url: string, localize?: boolean): Promise<{ aweme_id: string; status: string }> {
  return request<{ aweme_id: string; status: string }>('/api/videos', {
    method: 'POST',
    body: JSON.stringify(localize === undefined ? { url } : { url, localize }),
  });
}

export async function cancelJob(id: string): Promise<JobResponse> {
  return request<JobResponse>(`/api/jobs/${id}/cancel`, { method: 'POST' });
}

export async function retryJob(id: string): Promise<{ job: JobResponse; videos_reset: number }> {
  return request<{ job: JobResponse; videos_reset: number }>(`/api/jobs/${id}/retry`, { method: 'POST' });
}

export async function getCookieStatus(): Promise<CookieStatus> {
  return request<CookieStatus>('/internal/cookie/status');
}

function queryString(query: VideoListQuery): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== '') params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : '';
}

export async function getDashboardVideos(query: VideoListQuery = {}): Promise<VideoListResponse> {
  return request<VideoListResponse>(`/api/dashboard/videos${queryString(query)}`);
}

export async function getDashboardVideo(awemeId: string): Promise<VideoDetailResponse> {
  return request<VideoDetailResponse>(`/api/dashboard/videos/${encodeURIComponent(awemeId)}`);
}

export async function getAttention(): Promise<AttentionResponse> {
  return request<AttentionResponse>('/api/dashboard/attention');
}

export async function getChannels(): Promise<ChannelListResponse> {
  return request<ChannelListResponse>('/api/dashboard/channels');
}

export async function getHealth(): Promise<HealthReport> {
  return request<HealthReport>('/api/dashboard/health');
}

export async function getAutoQueue(): Promise<AutoQueueStatus> {
  return request<AutoQueueStatus>('/api/dashboard/auto-queue');
}

/** Turning it on starts the next held video at once when none is running. */
export async function setAutoQueue(enabled: boolean): Promise<AutoQueueStatus> {
  return request<AutoQueueStatus>('/api/dashboard/auto-queue', { method: 'PUT', body: JSON.stringify({ enabled }) });
}

export async function runAction(body: ActionRequest): Promise<ActionResult> {
  return request<ActionResult>('/api/dashboard/actions', { method: 'POST', body: JSON.stringify(body) });
}

/** Signed, short-lived URLs; each is prefixed with the API base so it works from any origin setup. */
export async function getMediaLinks(awemeId: string, generation: number, kinds: MediaKind[]): Promise<MediaLink[]> {
  const res = await request<{ links: MediaLink[] }>('/api/dashboard/media-links', {
    method: 'POST',
    body: JSON.stringify({ aweme_id: awemeId, generation, kinds }),
  });
  return res.links.map((l) => (l.url ? { ...l, url: `${baseUrl()}${l.url}` } : l));
}

export async function setAutoLocalize(secUserId: string, autoLocalize: boolean): Promise<void> {
  await request(`/api/profiles/${encodeURIComponent(secUserId)}`, {
    method: 'PATCH',
    body: JSON.stringify({ auto_localize: autoLocalize }),
  });
}

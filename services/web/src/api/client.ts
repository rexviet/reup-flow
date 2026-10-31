import type { CookieStatus, JobListResponse, JobResponse, RunDetail, WorkspaceSnapshot } from './types';

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

function baseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL.replace(/\/$/, '');
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

export async function createJob(url: string): Promise<{ job_id: string }> {
  return request<{ job_id: string }>('/api/jobs', {
    method: 'POST',
    body: JSON.stringify({ url }),
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

export async function getWorkspaceSnapshot(): Promise<WorkspaceSnapshot> {
  return request<WorkspaceSnapshot>('/api/workspace/snapshot');
}

export async function getRunDetail(awemeId: string, generation: number): Promise<RunDetail> {
  return request<RunDetail>(`/api/workspace/runs/${encodeURIComponent(awemeId)}/${generation}`);
}
